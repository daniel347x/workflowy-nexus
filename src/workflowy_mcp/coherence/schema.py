"""Versioned durable coherence schemas (S01 §6).

Every durable object carries integer ``schema_version: 1``, an exact account
scope, strict required fields, JSON-safe values, and deterministic
``to_dict`` / ``from_dict`` behavior.

Fail-closed doctrine:

- a missing or non-integer version is malformed (``CoherenceSchemaError``);
- any version other than 1 raises ``UnsupportedCoherenceSchemaError``;
- booleans never satisfy integer fields;
- timestamps are timezone-aware UTC ISO-8601 strings;
- unknown keys on CONTROL artifacts (manifest, journal transaction
  envelopes, effect envelopes, dirty-barrier state, external-dirt state)
  fail closed.  Canonical NODE payloads embedded inside effects are the
  documented extensibility surface: unknown node/cache-metadata keys there
  are preserved by the §7 codec, never rejected (audit amendment F4).
"""

from __future__ import annotations

import json
import math
import secrets
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from .. import carto_job_governance as gov
from .errors import (
    CoherenceAccountMismatchError,
    CoherenceError,
    CoherenceSchemaError,
    CoherenceStateMissingError,
    ManifestConflictError,
    UnsupportedCoherenceSchemaError,
)
from .paths import CoherencePaths, validate_account_scope_id

SCHEMA_VERSION = 1

_COHERENCE_MODES = frozenset({"bootstrap", "transition", "enforced"})
_OWNER_KINDS = frozenset({"process", "detached_worker", "system"})
_OWNERSHIP_KINDS = frozenset({"none", "lease", "claim"})
_DIRT_MUTATION_KINDS = frozenset({"input", "structural", "reconnect", "synthetic"})
_DETAIL_LIMIT = 500


# ---------------------------------------------------------------------------
# Shared strict-field helpers
# ---------------------------------------------------------------------------


def _artifact_version(payload: Mapping[str, Any], artifact: str, *, path: Any = None) -> int:
    version = payload.get("schema_version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise CoherenceSchemaError(
            f"{artifact}: schema_version missing or not an integer", path=path
        )
    if version != SCHEMA_VERSION:
        raise UnsupportedCoherenceSchemaError(
            f"{artifact}: unsupported schema_version {version}", path=path
        )
    return version


def _reject_unknown_keys(
    payload: Mapping[str, Any], allowed: Iterable[str], artifact: str, *, path: Any = None
) -> None:
    extra = sorted(set(payload) - set(allowed))
    if extra:
        raise CoherenceSchemaError(
            f"{artifact}: unknown keys fail closed: {extra}", path=path
        )


def _require_int(
    payload: Mapping[str, Any],
    key: str,
    artifact: str,
    *,
    minimum: int | None = None,
    path: Any = None,
) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise CoherenceSchemaError(f"{artifact}: {key} must be an integer", path=path)
    if minimum is not None and value < minimum:
        raise CoherenceSchemaError(
            f"{artifact}: {key} must be >= {minimum}", path=path
        )
    return value


def _require_bool(payload: Mapping[str, Any], key: str, artifact: str, *, path: Any = None) -> bool:
    value = payload.get(key)
    if not isinstance(value, bool):
        raise CoherenceSchemaError(f"{artifact}: {key} must be a boolean", path=path)
    return value


def _require_str(payload: Mapping[str, Any], key: str, artifact: str, *, path: Any = None) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise CoherenceSchemaError(f"{artifact}: {key} must be a nonempty string", path=path)
    return value


def _optional_str(payload: Mapping[str, Any], key: str, artifact: str, *, path: Any = None) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise CoherenceSchemaError(f"{artifact}: {key} must be a string or null", path=path)
    return value


def _require_timestamp(value: Any, key: str, artifact: str, *, path: Any = None) -> str:
    """Validate a timezone-aware UTC ISO-8601 timestamp string."""

    if not isinstance(value, str) or not value.strip():
        raise CoherenceSchemaError(f"{artifact}: {key} must be a timestamp string", path=path)
    raw = value.strip()
    candidate = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise CoherenceSchemaError(
            f"{artifact}: {key} is not ISO-8601: {raw!r}", path=path
        ) from exc
    if parsed.tzinfo is None:
        raise CoherenceSchemaError(
            f"{artifact}: {key} must be timezone-aware UTC", path=path
        )
    if parsed.utcoffset() != timezone.utc.utcoffset(None):
        raise CoherenceSchemaError(
            f"{artifact}: {key} must be UTC", path=path
        )
    return raw


def _optional_timestamp(value: Any, key: str, artifact: str, *, path: Any = None) -> str | None:
    if value is None:
        return None
    return _require_timestamp(value, key, artifact, path=path)


def _require_json_safe(value: Any, key: str, artifact: str, *, path: Any = None) -> Any:
    try:
        json.dumps(value)
    except (TypeError, ValueError) as exc:
        raise CoherenceSchemaError(
            f"{artifact}: {key} must be JSON-safe", path=path
        ) from exc
    return value


def _bound_detail(value: str | None) -> str | None:
    if value is not None and len(value) > _DETAIL_LIMIT:
        return value[:_DETAIL_LIMIT]
    return value


def _optional_str_field(value: Any, key: str, artifact: str) -> str | None:
    """Constructor-side strict optional-string validation (A9)."""

    if value is None:
        return None
    if not isinstance(value, str):
        raise CoherenceSchemaError(f"{artifact}: {key} must be a string or null")
    return value


def _attach_context(
    exc: BaseException, *, path: Any = None, account: str | None = None
) -> BaseException:
    """Re-attach known artifact context to constructor-raised errors (so a
    malformed on-disk artifact never reports path=None)."""

    if isinstance(exc, CoherenceError):
        additions: list[str] = []
        if exc.path is None and path is not None:
            exc.path = str(path)
            additions.append(f"path={exc.path}")
        if exc.account_scope_id is None and account is not None:
            exc.account_scope_id = account
            additions.append(f"account={account}")
        if additions and exc.args:
            exc.args = (f"{exc.args[0]} (+{', '.join(additions)})",)
    return exc


def _new_epoch_id() -> str:
    return secrets.token_hex(16)


def utc_now() -> str:
    return gov.utc_now_iso()


# ---------------------------------------------------------------------------
# Hard dirty barrier v1 (nested in the manifest) — §6.2
# ---------------------------------------------------------------------------


@dataclass(frozen=True, init=False)
class DirtyBarrierState:
    """Durable hard coherence failure state.  S01 validates/constructs only;
    S04 owns production writes/enforcement.

    The public constructor surface is the SPECIFIED ``affected_region=``
    keyword; the value is stored deep-owned in a private field and escapes
    only as a deep copy through the ``affected_region`` property.
    """

    schema_version: int
    account_scope_id: str
    active: bool
    generation: int
    reason_code: str | None
    detail: str | None
    transaction_id: str | None
    # Deeply immutable by ownership (S1): the region never escapes except as
    # a deep copy through the ``affected_region`` property below.
    _affected_region: dict | None
    raised_at: str | None

    _KEYS = (
        "schema_version",
        "account_scope_id",
        "active",
        "generation",
        "reason_code",
        "detail",
        "transaction_id",
        "affected_region",
        "raised_at",
    )

    def __init__(
        self,
        *,
        schema_version: int,
        account_scope_id: str,
        active: bool,
        generation: int,
        reason_code: str | None,
        detail: str | None,
        transaction_id: str | None,
        affected_region: dict | None,
        raised_at: str | None,
    ) -> None:
        import copy as _copy

        object.__setattr__(self, "schema_version", schema_version)
        object.__setattr__(self, "account_scope_id", account_scope_id)
        object.__setattr__(self, "active", active)
        object.__setattr__(self, "generation", generation)
        object.__setattr__(self, "reason_code", reason_code)
        object.__setattr__(self, "detail", detail)
        object.__setattr__(self, "transaction_id", transaction_id)
        object.__setattr__(
            self,
            "_affected_region",
            None if affected_region is None else _copy.deepcopy(affected_region),
        )
        object.__setattr__(self, "raised_at", raised_at)
        self._validate()

    @property
    def affected_region(self) -> dict | None:
        """Deeply immutable by ownership: every read returns a deep copy."""

        import copy as _copy

        return _copy.deepcopy(self._affected_region)

    def _validate(self) -> None:
        if isinstance(self.schema_version, bool) or self.schema_version != SCHEMA_VERSION:
            raise CoherenceSchemaError("dirty barrier: schema_version must be 1")
        validate_account_scope_id(self.account_scope_id)
        if not isinstance(self.active, bool):
            raise CoherenceSchemaError("dirty barrier: active must be a boolean")
        if isinstance(self.generation, bool) or not isinstance(self.generation, int) or self.generation < 0:
            raise CoherenceSchemaError("dirty barrier: generation must be a non-negative integer")
        _optional_str_field(self.reason_code, "reason_code", "dirty barrier")
        _optional_str_field(self.detail, "detail", "dirty barrier")
        _optional_str_field(self.transaction_id, "transaction_id", "dirty barrier")
        if self.active:
            if not self.reason_code:
                raise CoherenceSchemaError("dirty barrier: active state requires reason_code")
            if not self.raised_at:
                raise CoherenceSchemaError("dirty barrier: active state requires raised_at")
        if self._affected_region is not None:
            if not isinstance(self._affected_region, dict):
                raise CoherenceSchemaError("dirty barrier: affected_region must be an object or null")
            _require_json_safe(self._affected_region, "affected_region", "dirty barrier")
        if self.raised_at is not None:
            _require_timestamp(self.raised_at, "raised_at", "dirty barrier")

    @classmethod
    def clean(cls, account_scope_id: str) -> DirtyBarrierState:
        """The clean initial state: generation 0, all diagnostic fields null."""

        return cls(
            schema_version=SCHEMA_VERSION,
            account_scope_id=validate_account_scope_id(account_scope_id),
            active=False,
            generation=0,
            reason_code=None,
            detail=None,
            transaction_id=None,
            affected_region=None,
            raised_at=None,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "account_scope_id": self.account_scope_id,
            "active": self.active,
            "generation": self.generation,
            "reason_code": self.reason_code,
            "detail": self.detail,
            "transaction_id": self.transaction_id,
            "affected_region": self.affected_region,
            "raised_at": self.raised_at,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any], *, path: Any = None) -> DirtyBarrierState:
        _artifact_version(payload, "dirty barrier", path=path)
        _reject_unknown_keys(payload, cls._KEYS, "dirty barrier", path=path)
        account = payload.get("account_scope_id")
        if not isinstance(account, str):
            raise CoherenceSchemaError("dirty barrier: account_scope_id required", path=path)
        try:
            return cls(
                schema_version=SCHEMA_VERSION,
                account_scope_id=validate_account_scope_id(account),
                active=_require_bool(payload, "active", "dirty barrier", path=path),
                generation=_require_int(payload, "generation", "dirty barrier", minimum=0, path=path),
                reason_code=_optional_str(payload, "reason_code", "dirty barrier", path=path),
                detail=_optional_str(payload, "detail", "dirty barrier", path=path),
                transaction_id=_optional_str(payload, "transaction_id", "dirty barrier", path=path),
                affected_region=payload.get("affected_region"),
                raised_at=_optional_timestamp(
                    payload.get("raised_at"), "raised_at", "dirty barrier", path=path
                ),
            )
        except CoherenceError as exc:
            raise _attach_context(exc, path=path, account=account) from exc


# ---------------------------------------------------------------------------
# External dirt v1 — physically and semantically separate — §6.3
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
# @beacon[
#   id=auto-beacon@ExternalDirtState-3j99,
#   role=ExternalDirtState,
#   slice_labels=mci-barrier-dirt,
#   kind=ast,
# ]
class ExternalDirtState:
    """Shared GUI/external-edit dirt: stale-but-uncorrupted (refresh
    recommended).  NEVER conflated with the hard dirty barrier."""

    schema_version: int
    account_scope_id: str
    active: bool
    generation: int
    observed_at: str | None
    mutation_kind: Literal["input", "structural", "reconnect", "synthetic"] | None
    node_uuid: str | None
    parent_uuid: str | None
    detail: str | None

    _KEYS = (
        "schema_version",
        "account_scope_id",
        "active",
        "generation",
        "observed_at",
        "mutation_kind",
        "node_uuid",
        "parent_uuid",
        "detail",
    )

    def __post_init__(self) -> None:
        if isinstance(self.schema_version, bool) or self.schema_version != SCHEMA_VERSION:
            raise CoherenceSchemaError("external dirt: schema_version must be 1")
        validate_account_scope_id(self.account_scope_id)
        if not isinstance(self.active, bool):
            raise CoherenceSchemaError("external dirt: active must be a boolean")
        if isinstance(self.generation, bool) or not isinstance(self.generation, int) or self.generation < 0:
            raise CoherenceSchemaError("external dirt: generation must be a non-negative integer")
        if self.mutation_kind is not None and self.mutation_kind not in _DIRT_MUTATION_KINDS:
            raise CoherenceSchemaError(
                f"external dirt: mutation_kind must be one of {sorted(_DIRT_MUTATION_KINDS)} or null"
            )
        _optional_str_field(self.node_uuid, "node_uuid", "external dirt")
        _optional_str_field(self.parent_uuid, "parent_uuid", "external dirt")
        _optional_str_field(self.detail, "detail", "external dirt")
        if self.active and not self.observed_at:
            raise CoherenceSchemaError("external dirt: active state requires observed_at")
        if self.observed_at is not None:
            _require_timestamp(self.observed_at, "observed_at", "external dirt")

    @classmethod
    def clean(cls, account_scope_id: str) -> ExternalDirtState:
        return cls(
            schema_version=SCHEMA_VERSION,
            account_scope_id=validate_account_scope_id(account_scope_id),
            active=False,
            generation=0,
            observed_at=None,
            mutation_kind=None,
            node_uuid=None,
            parent_uuid=None,
            detail=None,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "account_scope_id": self.account_scope_id,
            "active": self.active,
            "generation": self.generation,
            "observed_at": self.observed_at,
            "mutation_kind": self.mutation_kind,
            "node_uuid": self.node_uuid,
            "parent_uuid": self.parent_uuid,
            "detail": self.detail,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any], *, path: Any = None) -> ExternalDirtState:
        _artifact_version(payload, "external dirt", path=path)
        _reject_unknown_keys(payload, cls._KEYS, "external dirt", path=path)
        account = payload.get("account_scope_id")
        if not isinstance(account, str):
            raise CoherenceSchemaError("external dirt: account_scope_id required", path=path)
        try:
            return cls(
                schema_version=SCHEMA_VERSION,
                account_scope_id=validate_account_scope_id(account),
                active=_require_bool(payload, "active", "external dirt", path=path),
                generation=_require_int(payload, "generation", "external dirt", minimum=0, path=path),
                observed_at=_optional_timestamp(
                    payload.get("observed_at"), "observed_at", "external dirt", path=path
                ),
                mutation_kind=payload.get("mutation_kind"),
                node_uuid=_optional_str(payload, "node_uuid", "external dirt", path=path),
                parent_uuid=_optional_str(payload, "parent_uuid", "external dirt", path=path),
                detail=_optional_str(payload, "detail", "external dirt", path=path),
            )
        except CoherenceError as exc:
            raise _attach_context(exc, path=path, account=account) from exc


# ---------------------------------------------------------------------------
# Coherence Manifest v1 — §6.1
# ---------------------------------------------------------------------------


# @beacon[
#   id=coherence@schema-models,
#   slice_labels=carto-governance,mci-epoch-schema,
#   kind=ast,
#   comment=S01 durable manifest authority model: account/epoch identity (with durable previous_epoch_id for post-cut lineage), base incorporation watermark, journal floor/head, coherence mode, nested hard dirty barrier; strict v1 validation with every lineage invariant fail-closed. One obsolete receipt key is tolerated only on read and never emitted.,
# ]
@dataclass(frozen=True)
class CoherenceManifest:
    """The Coherence Manifest: account/epoch identity, base snapshot lineage,
    journal watermarks, coherence mode and the nested hard dirty barrier.
    One obsolete receipt key is tolerated only on read and never emitted."""

    schema_version: int
    account_scope_id: str
    epoch_number: int
    epoch_id: str
    base_snapshot_path: str | None
    base_incorporated_through_seq: int
    journal_floor_seq: int
    journal_head_seq: int
    coherence_mode: Literal["bootstrap", "transition", "enforced"]
    dirty_status: DirtyBarrierState
    created_at: str
    updated_at: str
    # Durable immediate-predecessor epoch identity (S02 forward-blocking
    # lineage fix): set by the epoch publisher at a cut; None in bootstrap.
    previous_epoch_id: str | None = None

    _KEYS = (
        "schema_version",
        "account_scope_id",
        "epoch_number",
        "epoch_id",
        "base_snapshot_path",
        "base_incorporated_through_seq",
        "journal_floor_seq",
        "journal_head_seq",
        "coherence_mode",
        "dirty_status",
        "created_at",
        "updated_at",
        "previous_epoch_id",
    )

    def __post_init__(self) -> None:
        if isinstance(self.schema_version, bool) or self.schema_version != SCHEMA_VERSION:
            raise CoherenceSchemaError("manifest: schema_version must be 1")
        validate_account_scope_id(self.account_scope_id)
        for key in (
            "epoch_number",
            "base_incorporated_through_seq",
            "journal_floor_seq",
            "journal_head_seq",
        ):
            value = getattr(self, key)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise CoherenceSchemaError(f"manifest: {key} must be a non-negative integer")
        if not isinstance(self.epoch_id, str) or not self.epoch_id:
            raise CoherenceSchemaError("manifest: epoch_id must be a nonempty string")
        # Lineage invariants for the durable predecessor (R2/S2): epoch 0
        # (bootstrap) NEVER has a predecessor; every post-cut epoch (>= 1)
        # MUST carry a nonempty predecessor distinct from the current epoch.
        if self.epoch_number == 0:
            if self.previous_epoch_id is not None:
                raise CoherenceSchemaError(
                    "manifest: epoch 0 cannot carry a previous_epoch_id"
                )
        else:
            if not isinstance(self.previous_epoch_id, str) or not self.previous_epoch_id:
                raise CoherenceSchemaError(
                    "manifest: post-cut epochs require a nonempty previous_epoch_id"
                )
            if self.previous_epoch_id == self.epoch_id:
                raise CoherenceSchemaError(
                    "manifest: previous_epoch_id must differ from epoch_id"
                )
        if self.coherence_mode not in _COHERENCE_MODES:
            raise CoherenceSchemaError(
                f"manifest: coherence_mode must be one of {sorted(_COHERENCE_MODES)}"
            )
        # Lineage invariants (S01 §6.1).
        if self.base_incorporated_through_seq > self.journal_head_seq:
            raise CoherenceSchemaError(
                "manifest: base_incorporated_through_seq must be <= journal_head_seq"
            )
        if self.journal_floor_seq != self.base_incorporated_through_seq + 1:
            raise CoherenceSchemaError(
                "manifest: journal_floor_seq must equal base_incorporated_through_seq + 1"
            )
        # An empty active journal is floor == head + 1 (i.e. incorporated == head).
        if self.base_snapshot_path is None:
            if self.coherence_mode != "bootstrap":
                raise CoherenceSchemaError(
                    "manifest: base_snapshot_path is null only in bootstrap mode"
                )
        else:
            import os as _os

            if not isinstance(self.base_snapshot_path, str) or not _os.path.isabs(
                self.base_snapshot_path
            ):
                raise CoherenceSchemaError(
                    "manifest: base_snapshot_path must be an absolute normalized path"
                )
            normalized = _os.path.normpath(self.base_snapshot_path)
            if normalized != self.base_snapshot_path:
                raise CoherenceSchemaError(
                    "manifest: base_snapshot_path must be normalized"
                )
        if self.dirty_status.account_scope_id != self.account_scope_id:
            raise CoherenceAccountMismatchError(
                "manifest: dirty_status account does not match manifest account",
                account_scope_id=self.account_scope_id,
                epoch_id=self.epoch_id,
            )
        _require_timestamp(self.created_at, "created_at", "manifest")
        _require_timestamp(self.updated_at, "updated_at", "manifest")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "account_scope_id": self.account_scope_id,
            "epoch_number": self.epoch_number,
            "epoch_id": self.epoch_id,
            "base_snapshot_path": self.base_snapshot_path,
            "base_incorporated_through_seq": self.base_incorporated_through_seq,
            "journal_floor_seq": self.journal_floor_seq,
            "journal_head_seq": self.journal_head_seq,
            "coherence_mode": self.coherence_mode,
            "dirty_status": self.dirty_status.to_dict(),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "previous_epoch_id": self.previous_epoch_id,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any], *, path: Any = None) -> CoherenceManifest:
        _artifact_version(payload, "manifest", path=path)
        payload = {k: v for k, v in payload.items() if k != "covered_barrier_required_lease_ids"}
        _reject_unknown_keys(payload, cls._KEYS, "manifest", path=path)
        account = payload.get("account_scope_id")
        if not isinstance(account, str):
            raise CoherenceSchemaError("manifest: account_scope_id required", path=path)
        dirty = payload.get("dirty_status")
        if not isinstance(dirty, Mapping):
            raise CoherenceSchemaError("manifest: dirty_status must be an object", path=path)
        base_path = payload.get("base_snapshot_path")
        if base_path is not None and not isinstance(base_path, str):
            raise CoherenceSchemaError(
                "manifest: base_snapshot_path must be a string or null", path=path
            )
        try:
            return cls(
                schema_version=SCHEMA_VERSION,
                account_scope_id=validate_account_scope_id(account),
                epoch_number=_require_int(payload, "epoch_number", "manifest", minimum=0, path=path),
                epoch_id=_require_str(payload, "epoch_id", "manifest", path=path),
                base_snapshot_path=base_path,
                base_incorporated_through_seq=_require_int(
                    payload, "base_incorporated_through_seq", "manifest", minimum=0, path=path
                ),
                journal_floor_seq=_require_int(
                    payload, "journal_floor_seq", "manifest", minimum=0, path=path
                ),
                journal_head_seq=_require_int(
                    payload, "journal_head_seq", "manifest", minimum=0, path=path
                ),
                coherence_mode=payload.get("coherence_mode"),
                dirty_status=DirtyBarrierState.from_dict(dirty, path=path),
                created_at=_require_timestamp(
                    payload.get("created_at"), "created_at", "manifest", path=path
                ),
                updated_at=_require_timestamp(
                    payload.get("updated_at"), "updated_at", "manifest", path=path
                ),
                previous_epoch_id=_optional_str(
                    payload, "previous_epoch_id", "manifest", path=path
                ),
            )
        except CoherenceError as exc:
            raise _attach_context(exc, path=path, account=account) from exc


# @beacon[
#   id=coherence@manifest-constructors,
#   slice_labels=carto-governance,mci-epoch-schema,
#   kind=ast,
#   comment=S01 manifest constructors and CAS-guarded IO: bootstrap factory, validated pure updater, strict read, compare-and-swap write, O_EXCL first-writer initialization.,
# ]
def new_bootstrap_manifest(account_scope_id: str, now: str | None = None) -> CoherenceManifest:
    """Create the bootstrap manifest for one account scope.

    Bootstrap has no base snapshot (``base_snapshot_path=None``), epoch
    number 0, and an empty journal (floor 1, head 0, incorporated 0).
    """

    scope = validate_account_scope_id(account_scope_id)
    stamp = now or utc_now()
    return CoherenceManifest(
        schema_version=SCHEMA_VERSION,
        account_scope_id=scope,
        epoch_number=0,
        epoch_id=_new_epoch_id(),
        base_snapshot_path=None,
        base_incorporated_through_seq=0,
        journal_floor_seq=1,
        journal_head_seq=0,
        coherence_mode="bootstrap",
        dirty_status=DirtyBarrierState.clean(scope),
        created_at=stamp,
        updated_at=stamp,
    )


def manifest_with_updates(
    manifest: CoherenceManifest, now: str | None = None, **changes: Any
) -> CoherenceManifest:
    """Return a NEW validated manifest with the supplied field changes.

    ``updated_at`` refreshes automatically unless explicitly supplied.
    """

    unknown = sorted(set(changes) - set(CoherenceManifest._KEYS) - {"updated_at"})
    if unknown:
        raise CoherenceSchemaError(f"manifest: cannot update unknown fields {unknown}")
    if "schema_version" in changes:
        raise CoherenceSchemaError("manifest: schema_version cannot change")
    payload = manifest.to_dict()
    dirty_payload = payload.pop("dirty_status")
    payload.update(changes)
    if "updated_at" not in changes:
        payload["updated_at"] = now or utc_now()
    dirty = changes.get("dirty_status")
    if isinstance(dirty, DirtyBarrierState):
        payload["dirty_status"] = dirty
    elif isinstance(dirty, Mapping):
        payload["dirty_status"] = DirtyBarrierState.from_dict(dirty)
    else:
        payload["dirty_status"] = DirtyBarrierState.from_dict(dirty_payload)
    return CoherenceManifest(**payload)


def read_manifest(paths: CoherencePaths, *, required: bool = True) -> CoherenceManifest | None:
    """Read and strictly validate the manifest.  Missing state is a bootstrap
    concern (``CoherenceStateMissingError``) unless ``required=False``."""

    if not paths.manifest_path.exists():
        if required:
            raise CoherenceStateMissingError(
                "coherence manifest is not initialized",
                path=paths.manifest_path,
            )
        return None
    try:
        with paths.manifest_path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except json.JSONDecodeError as exc:
        raise CoherenceSchemaError(
            f"manifest: malformed JSON: {exc}", path=paths.manifest_path
        ) from exc
    if not isinstance(payload, Mapping):
        raise CoherenceSchemaError(
            "manifest: top-level JSON must be an object", path=paths.manifest_path
        )
    manifest = CoherenceManifest.from_dict(payload, path=paths.manifest_path)
    if manifest.account_scope_id != _account_from_paths(paths):
        raise CoherenceAccountMismatchError(
            "manifest account does not match the coherence paths account root",
            path=paths.manifest_path,
            account_scope_id=manifest.account_scope_id,
        )
    return manifest


def write_manifest(
    paths: CoherencePaths,
    manifest: CoherenceManifest,
    *,
    expected_epoch_id: str | None = None,
    expected_head_seq: int | None = None,
    fs_ops: Any = None,
) -> CoherenceManifest:
    """Atomically replace the manifest with compare-and-swap guards.

    When expected values are supplied the on-disk manifest must match them;
    stale writers receive ``ManifestConflictError`` and never overwrite.
    Callers hold the coherence mutex around read-check-write sequences.
    """

    from .durable_io import atomic_write_json_durable

    if manifest.account_scope_id != _account_from_paths(paths):
        raise CoherenceAccountMismatchError(
            "manifest account does not match the coherence paths account root",
            path=paths.manifest_path,
            account_scope_id=manifest.account_scope_id,
        )
    if expected_epoch_id is not None or expected_head_seq is not None:
        current = read_manifest(paths, required=True)
        assert current is not None
        if expected_epoch_id is not None and current.epoch_id != expected_epoch_id:
            raise ManifestConflictError(
                "manifest epoch changed underneath writer",
                path=paths.manifest_path,
                account_scope_id=manifest.account_scope_id,
                epoch_id=manifest.epoch_id,
            )
        if expected_head_seq is not None and current.journal_head_seq != expected_head_seq:
            raise ManifestConflictError(
                "manifest journal head changed underneath writer",
                path=paths.manifest_path,
                account_scope_id=manifest.account_scope_id,
                epoch_id=manifest.epoch_id,
                sequence=current.journal_head_seq,
            )
    atomic_write_json_durable(paths.manifest_path, manifest.to_dict(), fs_ops=fs_ops)
    return manifest


def initialize_manifest_if_absent(
    paths: CoherencePaths, manifest: CoherenceManifest
) -> CoherenceManifest:
    """First-manifest initialization runs to completion on the daemon loop
    before publication can interleave; returns the existing validated manifest
    or durably writes the first one. No mutex file is seeded.
    """

    from .paths import ensure_coherence_directories

    ensure_coherence_directories(paths)
    if manifest.account_scope_id != _account_from_paths(paths):
        raise CoherenceAccountMismatchError(
            "manifest account does not match the coherence paths account root",
            path=paths.manifest_path,
            account_scope_id=manifest.account_scope_id,
        )
    existing = read_manifest(paths, required=False)
    if existing is not None:
        if existing.account_scope_id != manifest.account_scope_id:
            raise CoherenceAccountMismatchError(
                "existing manifest belongs to another account scope",
                path=paths.manifest_path,
                account_scope_id=manifest.account_scope_id,
            )
        return existing
    from .durable_io import atomic_write_json_durable

    atomic_write_json_durable(paths.manifest_path, manifest.to_dict())
    return manifest


# ---------------------------------------------------------------------------
# External dirt IO — §6.3 (primitives only; no callers wired yet)
# ---------------------------------------------------------------------------


# @beacon[
#   id=auto-beacon@read_external_dirt-zm20,
#   role=read_external_dirt,
#   slice_labels=mci-barrier-dirt,
#   kind=ast,
# ]
def read_external_dirt(
    paths: CoherencePaths, *, required: bool = False
) -> ExternalDirtState | None:
    """Read external dirt state.  Missing state yields the clean generation-0
    state unless ``required=True``."""

    if not paths.external_dirt_path.exists():
        if required:
            raise CoherenceStateMissingError(
                "external dirt state is not initialized",
                path=paths.external_dirt_path,
            )
        return ExternalDirtState.clean(_account_from_paths(paths))
    try:
        with paths.external_dirt_path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except json.JSONDecodeError as exc:
        raise CoherenceSchemaError(
            f"external dirt: malformed JSON: {exc}", path=paths.external_dirt_path
        ) from exc
    if not isinstance(payload, Mapping):
        raise CoherenceSchemaError(
            "external dirt: top-level JSON must be an object", path=paths.external_dirt_path
        )
    state = ExternalDirtState.from_dict(payload, path=paths.external_dirt_path)
    if state.account_scope_id != _account_from_paths(paths):
        raise CoherenceAccountMismatchError(
            "external dirt account does not match the coherence paths account root",
            path=paths.external_dirt_path,
            account_scope_id=state.account_scope_id,
        )
    return state


# @beacon[
#   id=auto-beacon@write_external_dirt-qxzi,
#   role=write_external_dirt,
#   slice_labels=mci-barrier-dirt,
#   kind=ast,
# ]
def write_external_dirt(
    paths: CoherencePaths, state: ExternalDirtState, *, fs_ops: Any = None
) -> ExternalDirtState:
    from .durable_io import atomic_write_json_durable

    if state.account_scope_id != _account_from_paths(paths):
        raise CoherenceAccountMismatchError(
            "external dirt account does not match the coherence paths account root",
            path=paths.external_dirt_path,
            account_scope_id=state.account_scope_id,
        )
    atomic_write_json_durable(paths.external_dirt_path, state.to_dict(), fs_ops=fs_ops)
    return state


def _account_from_paths(paths: CoherencePaths) -> str:
    return validate_account_scope_id(paths.account_root.name)


# ---------------------------------------------------------------------------
# Journal transaction envelope v1 — §6.4
# ---------------------------------------------------------------------------


# @beacon[
#   id=coherence@journal-models,
#   slice_labels=carto-governance,mci-journal-effects,
#   kind=ast,
#   comment=S01 journal envelope models: JournalProducer (secret-free ownership/governance provenance), bounded VerificationEvidence (diagnostic, never reducer authority), immutable JournalTransaction with construction-time JSON-safety.,
# ]
@dataclass(frozen=True)
class JournalProducer:
    """Who produced a record.  Carries no secret; supports later
    transaction-bound governance without schema replacement."""

    pid: int
    pid_create_time: float
    owner_kind: Literal["process", "detached_worker", "system"]
    ownership_kind: Literal["none", "lease", "claim"]
    ownership_token: str | None = None
    job_id: str | None = None

    _KEYS = (
        "pid",
        "pid_create_time",
        "owner_kind",
        "ownership_kind",
        "ownership_token",
        "job_id",
    )

    def __post_init__(self) -> None:
        if isinstance(self.pid, bool) or not isinstance(self.pid, int) or self.pid <= 0:
            raise CoherenceSchemaError("producer: pid must be a positive integer")
        if (
            isinstance(self.pid_create_time, bool)
            or not isinstance(self.pid_create_time, (int, float))
            or not math.isfinite(float(self.pid_create_time))
        ):
            raise CoherenceSchemaError("producer: pid_create_time must be finite")
        if self.owner_kind not in _OWNER_KINDS:
            raise CoherenceSchemaError(
                f"producer: owner_kind must be one of {sorted(_OWNER_KINDS)}"
            )
        if self.ownership_kind not in _OWNERSHIP_KINDS:
            raise CoherenceSchemaError(
                f"producer: ownership_kind must be one of {sorted(_OWNERSHIP_KINDS)}"
            )
        _optional_str_field(self.ownership_token, "ownership_token", "producer")
        _optional_str_field(self.job_id, "job_id", "producer")

    def to_dict(self) -> dict[str, Any]:
        return {
            "pid": self.pid,
            "pid_create_time": float(self.pid_create_time),
            "owner_kind": self.owner_kind,
            "ownership_kind": self.ownership_kind,
            "ownership_token": self.ownership_token,
            "job_id": self.job_id,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any], *, path: Any = None) -> JournalProducer:
        _reject_unknown_keys(payload, cls._KEYS, "journal producer", path=path)
        pid = payload.get("pid")
        pid_create_time = payload.get("pid_create_time")
        try:
            return cls(
                pid=pid if isinstance(pid, int) else -1,
                pid_create_time=pid_create_time if isinstance(pid_create_time, (int, float)) else float("nan"),
                owner_kind=payload.get("owner_kind"),
                ownership_kind=payload.get("ownership_kind"),
                ownership_token=_optional_str(payload, "ownership_token", "journal producer", path=path),
                job_id=_optional_str(payload, "job_id", "journal producer", path=path),
            )
        except CoherenceError as exc:
            raise _attach_context(exc, path=path) from exc


@dataclass(frozen=True)
# @beacon[
#   id=auto-beacon@VerificationEvidence-xdr6,
#   role=VerificationEvidence,
#   slice_labels=mci-journal-effects,
#   kind=ast,
# ]
class VerificationEvidence:
    """Diagnostic, bounded, JSON-safe metadata.  NEVER the reducer authority;
    effects are."""

    kind: str
    outcome: str
    node_uuid: str | None = None
    parent_uuid: str | None = None
    detail: str | None = None

    _KEYS = ("kind", "outcome", "node_uuid", "parent_uuid", "detail")

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str) or not self.kind:
            raise CoherenceSchemaError("verification: kind must be a nonempty string")
        if not isinstance(self.outcome, str) or not self.outcome:
            raise CoherenceSchemaError("verification: outcome must be a nonempty string")
        _optional_str_field(self.node_uuid, "node_uuid", "verification")
        _optional_str_field(self.parent_uuid, "parent_uuid", "verification")
        _optional_str_field(self.detail, "detail", "verification")
        object.__setattr__(self, "detail", _bound_detail(self.detail))

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "outcome": self.outcome,
            "node_uuid": self.node_uuid,
            "parent_uuid": self.parent_uuid,
            "detail": self.detail,
        }

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, Any], *, path: Any = None
    ) -> VerificationEvidence:
        _reject_unknown_keys(payload, cls._KEYS, "verification evidence", path=path)
        try:
            return cls(
                kind=payload.get("kind"),
                outcome=payload.get("outcome"),
                node_uuid=_optional_str(payload, "node_uuid", "verification evidence", path=path),
                parent_uuid=_optional_str(payload, "parent_uuid", "verification evidence", path=path),
                detail=_optional_str(payload, "detail", "verification evidence", path=path),
            )
        except CoherenceError as exc:
            raise _attach_context(exc, path=path) from exc


@dataclass(frozen=True)
# @beacon[
#   id=auto-beacon@JournalTransaction-advq,
#   role=JournalTransaction,
#   slice_labels=mci-journal-effects,
#   kind=ast,
# ]
class JournalTransaction:
    """One committed journal record: verified canonical after-state effects,
    never HTTP intents.  Immutable once fsynced."""

    schema_version: int
    account_scope_id: str
    sequence: int
    previous_sequence: int
    transaction_id: str
    published_epoch_id: str
    operation_kind: str
    committed_at: str
    producer: JournalProducer
    verification: tuple[VerificationEvidence, ...]
    effects: tuple[Any, ...]  # tuple[SemanticEffect, ...] — typed loosely to avoid import cycle

    _KEYS = (
        "schema_version",
        "account_scope_id",
        "sequence",
        "previous_sequence",
        "transaction_id",
        "published_epoch_id",
        "operation_kind",
        "committed_at",
        "producer",
        "verification",
        "effects",
    )

    def __post_init__(self) -> None:
        if isinstance(self.schema_version, bool) or self.schema_version != SCHEMA_VERSION:
            raise CoherenceSchemaError("journal transaction: schema_version must be 1")
        validate_account_scope_id(self.account_scope_id)
        if isinstance(self.sequence, bool) or not isinstance(self.sequence, int) or self.sequence < 1:
            raise CoherenceSchemaError("journal transaction: sequence must be >= 1")
        if (
            isinstance(self.previous_sequence, bool)
            or not isinstance(self.previous_sequence, int)
            or self.previous_sequence != self.sequence - 1
        ):
            raise CoherenceSchemaError(
                "journal transaction: previous_sequence must equal sequence - 1"
            )
        if not isinstance(self.transaction_id, str) or not self.transaction_id:
            raise CoherenceSchemaError(
                "journal transaction: transaction_id must be collision-resistant opaque text"
            )
        if not isinstance(self.published_epoch_id, str) or not self.published_epoch_id:
            raise CoherenceSchemaError(
                "journal transaction: published_epoch_id must be nonempty"
            )
        if not isinstance(self.operation_kind, str) or not self.operation_kind:
            raise CoherenceSchemaError(
                "journal transaction: operation_kind must be a nonempty string"
            )
        _require_timestamp(self.committed_at, "committed_at", "journal transaction")
        if not isinstance(self.producer, JournalProducer):
            raise CoherenceSchemaError("journal transaction: producer invalid")
        for entry in self.verification:
            if not isinstance(entry, VerificationEvidence):
                raise CoherenceSchemaError(
                    "journal transaction: verification entries must be VerificationEvidence"
                )
        if not self.effects:
            raise CoherenceSchemaError("journal transaction: effects must be nonempty")
        for effect in self.effects:
            if not (hasattr(effect, "effect_type") and hasattr(effect, "to_dict")):
                raise CoherenceSchemaError(
                    "journal transaction: effects entries must be SemanticEffect objects"
                )
        # The whole envelope must be JSON-safe at CONSTRUCTION (A9): caller
        # schema bugs surface as typed schema errors here, never as physical
        # JournalPublicationError during the durable write.
        try:
            json.dumps(self.to_dict())
        except (TypeError, ValueError) as exc:
            raise CoherenceSchemaError(
                f"journal transaction: envelope is not JSON-safe: {exc}"
            ) from exc

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "account_scope_id": self.account_scope_id,
            "sequence": self.sequence,
            "previous_sequence": self.previous_sequence,
            "transaction_id": self.transaction_id,
            "published_epoch_id": self.published_epoch_id,
            "operation_kind": self.operation_kind,
            "committed_at": self.committed_at,
            "producer": self.producer.to_dict(),
            "verification": [item.to_dict() for item in self.verification],
            "effects": [effect.to_dict() for effect in self.effects],
        }

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, Any],
        *,
        effect_decoder: Callable[[Mapping[str, Any]], Any],
        path: Any = None,
    ) -> JournalTransaction:
        _artifact_version(payload, "journal transaction", path=path)
        _reject_unknown_keys(payload, cls._KEYS, "journal transaction", path=path)
        account = payload.get("account_scope_id")
        if not isinstance(account, str):
            raise CoherenceSchemaError(
                "journal transaction: account_scope_id required", path=path
            )
        producer = payload.get("producer")
        if not isinstance(producer, Mapping):
            raise CoherenceSchemaError(
                "journal transaction: producer must be an object", path=path
            )
        verification = payload.get("verification")
        if not isinstance(verification, list):
            raise CoherenceSchemaError(
                "journal transaction: verification must be an array", path=path
            )
        for entry in verification:
            if not isinstance(entry, Mapping):
                raise CoherenceSchemaError(
                    "journal transaction: verification entries must be objects", path=path
                )
        effects = payload.get("effects")
        if not isinstance(effects, list):
            raise CoherenceSchemaError(
                "journal transaction: effects must be an array", path=path
            )
        for entry in effects:
            if not isinstance(entry, Mapping):
                raise CoherenceSchemaError(
                    "journal transaction: effects entries must be objects", path=path
                )
        try:
            return cls(
                schema_version=SCHEMA_VERSION,
                account_scope_id=validate_account_scope_id(account),
                sequence=payload.get("sequence"),
                previous_sequence=payload.get("previous_sequence"),
                transaction_id=payload.get("transaction_id"),
                published_epoch_id=payload.get("published_epoch_id"),
                operation_kind=payload.get("operation_kind"),
                committed_at=payload.get("committed_at"),
                producer=JournalProducer.from_dict(producer, path=path),
                verification=tuple(
                    VerificationEvidence.from_dict(item, path=path) for item in verification
                ),
                effects=tuple(effect_decoder(item) for item in effects),
            )
        except CoherenceError as exc:
            raise _attach_context(exc, path=path, account=account) from exc


def new_transaction_id() -> str:
    """Collision-resistant opaque transaction identity."""

    return secrets.token_hex(16)


def new_epoch_id() -> str:
    """Collision-resistant opaque epoch identity (32 lowercase hex)."""

    return _new_epoch_id()


# ---------------------------------------------------------------------------
# In-memory refresh cut and publication vocabulary (S02 §5.2/§5.3)
# ---------------------------------------------------------------------------

# S02 refresh kinds, widened by S04 with ``checkpoint`` (the checkpoint
# rotation publisher reuses the ONE in-memory publication lifecycle, H2).
REFRESH_KINDS = frozenset(
    {
        "manual",
        "widget",
        "system",
        "bootstrap",
        "recovery",
        "confidence",
        "carto_preflight",
        "compat_patch",
        "checkpoint",
    }
)
FULL_REFRESH_KINDS = frozenset(
    {
        "manual",
        "widget",
        "system",
        "bootstrap",
        "recovery",
        "confidence",
        "carto_preflight",
    }
)
COMPAT_PATCH_KIND = "compat_patch"

REFRESH_CLAIM_ACTIVE_STATUSES = frozenset(
    {"claimed", "settling", "downloading", "materializing", "publishing"}
)
REFRESH_CLAIM_TERMINAL_STATUSES = frozenset({"completed", "failed"})
REFRESH_CLAIM_STATUSES = REFRESH_CLAIM_ACTIVE_STATUSES | REFRESH_CLAIM_TERMINAL_STATUSES


# @beacon[
#   id=auto-beacon@RefreshCutToken-sxy0,
#   role=RefreshCutToken,
#   slice_labels=mci-epoch-schema,
#   kind=ast,
#   comment=Immutable in-memory initiation cut; publication can clear only its captured hard-barrier and external-dirt generations.,
# ]
@dataclass(frozen=True)
class RefreshCutToken:
    """Immutable in-memory initiation cut; publication can clear only its
    captured hard-barrier and external-dirt generations. Captured under the
    coherence mutex before settle and fetch, so later signals survive."""

    account_scope_id: str
    epoch_number: int
    epoch_id: str
    journal_head_seq: int
    hard_barrier_generation: int
    external_dirt_generation: int
    captured_at: str

    def __post_init__(self) -> None:
        validate_account_scope_id(self.account_scope_id)
        for key in (
            "epoch_number",
            "journal_head_seq",
            "hard_barrier_generation",
            "external_dirt_generation",
        ):
            value = getattr(self, key)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise CoherenceSchemaError(
                    f"refresh cut: {key} must be a non-negative integer"
                )
        if not isinstance(self.epoch_id, str) or not self.epoch_id:
            raise CoherenceSchemaError("refresh cut: epoch_id must be a nonempty string")
        _require_timestamp(self.captured_at, "captured_at", "refresh cut")


def new_claim_id() -> str:
    """Collision-resistant opaque refresh-claim identity."""

    return secrets.token_hex(16)
