"""Snapshot envelope store (S02 §5.4 / §10 / §11).

One authority for the versioned on-disk snapshot wrapper: candidate
serialization (durable, atomic), strict envelope reads, manifest/base
lineage validation, journal-folded materialization, bootstrap-only legacy
discovery, and the manifest-aware GC protected-set computation.

Wrapper shape (v1)::

    {
      "snapshot_schema_version": 1,
      "snapshot_timestamp": "...",
      "account_scope_id": "...",
      "epoch_number": 4,
      "epoch_id": "...",
      "incorporated_through_seq": 1057,
      "publication_kind": "full_refresh | compat_patch | checkpoint | job_basis",
      "nodes_export": {"nodes": [...]}
    }

Legacy wrappers (``snapshot_timestamp`` + ``nodes_export`` only) are
accepted ONLY by the one-time no-manifest bootstrap path.  A
manifest-referenced base must always carry coherent lineage metadata.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import carto_job_governance as gov
from .durable_io import DurableFileOps, atomic_write_json_durable
from .effects import ApplicationMode, EffectWorkingCopy, apply_journal_transaction
from .errors import (
    CoherenceAccountMismatchError,
    CoherenceError,
    CoherenceSchemaError,
    CoherenceStateMissingError,
    JournalIntegrityError,
    UnsupportedCoherenceSchemaError,
)
from .journal_store import _read_record_file
from .paths import CoherencePaths, validate_account_scope_id
from .schema import CoherenceManifest, JournalTransaction, utc_now

SNAPSHOT_SCHEMA_VERSION = 1
PUBLICATION_KINDS = frozenset({"full_refresh", "compat_patch", "checkpoint", "job_basis"})

_SNAPSHOT_SUFFIX = "_nodes_export_snapshot.json"
_WRAPPER_KEYS = frozenset(
    {
        "snapshot_schema_version",
        "snapshot_timestamp",
        "account_scope_id",
        "epoch_number",
        "epoch_id",
        "incorporated_through_seq",
        "publication_kind",
        "nodes_export",
    }
)


def normalize_snapshot_path(path: str | os.PathLike[str]) -> str:
    """Absolute normalized path spelling suitable for manifest lineage."""

    return os.path.normpath(os.path.abspath(str(path)))


def new_snapshot_candidate_path(snapshot_dir: str | os.PathLike[str]) -> str:
    """A fresh candidate filename in the established timestamp+pid scheme.

    The prefix keeps time components in most-significant-first order so
    lexicographic sort equals chronological sort; the pid tiebreaker keeps
    concurrent writers collision-free.  The ``_nodes_export_snapshot.json``
    suffix keeps legacy enumeration working.
    """

    from datetime import datetime

    ts = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    return normalize_snapshot_path(
        Path(snapshot_dir) / f"{ts}_pid{os.getpid()}{_SNAPSHOT_SUFFIX}"
    )


@dataclass(frozen=True)
# @beacon[
#   id=auto-beacon@SnapshotEnvelope-d72n,
#   role=SnapshotEnvelope,
#   slice_labels=mci-epoch-snapshot,
#   kind=ast,
# ]
class SnapshotEnvelope:
    """One parsed snapshot wrapper with explicit lineage metadata."""

    path: str
    snapshot_timestamp: str | None
    nodes_export: dict[str, Any]
    legacy: bool
    snapshot_schema_version: int | None = None
    account_scope_id: str | None = None
    epoch_number: int | None = None
    epoch_id: str | None = None
    incorporated_through_seq: int | None = None
    publication_kind: str | None = None

    def lineage_dict(self) -> dict[str, Any]:
        """The lineage record carried into CARTO job payloads (§13)."""

        if self.legacy:
            raise CoherenceSchemaError(
                "legacy snapshot wrappers carry no lineage metadata", path=self.path
            )
        return {
            "snapshot_schema_version": self.snapshot_schema_version,
            "account_scope_id": self.account_scope_id,
            "epoch_number": self.epoch_number,
            "epoch_id": self.epoch_id,
            "incorporated_through_seq": self.incorporated_through_seq,
            "publication_kind": self.publication_kind,
        }


def _fail(message: str, *, path: Any = None) -> None:
    raise CoherenceSchemaError(f"snapshot envelope: {message}", path=path)


# @beacon[
#   id=coherence@snapshot-store,
#   slice_labels=ra-workflowy-cache,carto-governance,mci-epoch-snapshot,
#   kind=ast,
#   comment=Durable snapshot-envelope writes, strict lineage validation, manifest-base materialization through S2, and manifest/active-candidate GC protection; no per-job basis snapshot is produced.,
# ]
def write_snapshot_candidate(
    path: str | os.PathLike[str],
    nodes_export: Mapping[str, Any],
    *,
    account_scope_id: str,
    epoch_number: int,
    epoch_id: str,
    incorporated_through_seq: int,
    publication_kind: str,
    fs_ops: DurableFileOps | None = None,
    timestamp: str | None = None,
) -> str:
    """Serialize one fully-materialized candidate snapshot durably.

    A candidate is NEVER authoritative merely by existing: only
    ``epoch_publisher.publish_epoch`` makes a candidate the manifest base.
    Returns the absolute normalized candidate path.
    """

    scope = validate_account_scope_id(account_scope_id)
    if publication_kind not in PUBLICATION_KINDS:
        raise CoherenceSchemaError(
            f"snapshot candidate: publication_kind must be one of {sorted(PUBLICATION_KINDS)}"
        )
    if isinstance(epoch_number, bool) or not isinstance(epoch_number, int) or epoch_number < 0:
        raise CoherenceSchemaError("snapshot candidate: epoch_number must be a non-negative integer")
    if not isinstance(epoch_id, str) or not epoch_id:
        raise CoherenceSchemaError("snapshot candidate: epoch_id must be a nonempty string")
    if (
        isinstance(incorporated_through_seq, bool)
        or not isinstance(incorporated_through_seq, int)
        or incorporated_through_seq < 0
    ):
        raise CoherenceSchemaError(
            "snapshot candidate: incorporated_through_seq must be a non-negative integer"
        )
    if not isinstance(nodes_export, Mapping) or not isinstance(nodes_export.get("nodes"), list):
        raise CoherenceSchemaError("snapshot candidate: nodes_export must carry a 'nodes' list")
    target = normalize_snapshot_path(path)
    payload = {
        "snapshot_schema_version": SNAPSHOT_SCHEMA_VERSION,
        "snapshot_timestamp": timestamp or utc_now(),
        "account_scope_id": scope,
        "epoch_number": epoch_number,
        "epoch_id": epoch_id,
        "incorporated_through_seq": incorporated_through_seq,
        "publication_kind": publication_kind,
        "nodes_export": dict(nodes_export),
    }
    Path(target).parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json_durable(target, payload, fs_ops=fs_ops)
    return target


def read_snapshot_envelope(
    path: str | os.PathLike[str], *, allow_legacy: bool = False
) -> SnapshotEnvelope:
    """Strictly read and validate one snapshot wrapper.

    ``allow_legacy=True`` accepts the pre-S02 ``{snapshot_timestamp,
    nodes_export}`` shape for the one-time bootstrap path ONLY.  Every
    other caller receives fail-closed rejection of lineage-less wrappers.
    """

    target = Path(path)
    try:
        with target.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError as exc:
        raise CoherenceStateMissingError("snapshot file does not exist", path=target) from exc
    except json.JSONDecodeError as exc:
        raise CoherenceSchemaError(
            f"snapshot envelope: malformed JSON: {exc}", path=target
        ) from exc
    if not isinstance(payload, Mapping):
        _fail("top-level JSON must be an object", path=target)
    nodes_export = payload.get("nodes_export")
    if not isinstance(nodes_export, Mapping) or not isinstance(nodes_export.get("nodes"), list):
        _fail("missing a usable nodes_export object with a 'nodes' list", path=target)

    version = payload.get("snapshot_schema_version")
    if version is None:
        if not allow_legacy:
            raise CoherenceSchemaError(
                "snapshot envelope: legacy wrapper without lineage metadata is "
                "bootstrap-only and not accepted here",
                path=target,
            )
        timestamp = payload.get("snapshot_timestamp")
        if timestamp is not None and not isinstance(timestamp, str):
            _fail("legacy snapshot_timestamp must be a string or null", path=target)
        return SnapshotEnvelope(
            path=normalize_snapshot_path(target),
            snapshot_timestamp=timestamp,
            nodes_export=dict(nodes_export),
            legacy=True,
        )

    if isinstance(version, bool) or not isinstance(version, int):
        _fail("snapshot_schema_version missing or not an integer", path=target)
    if version != SNAPSHOT_SCHEMA_VERSION:
        raise UnsupportedCoherenceSchemaError(
            f"snapshot envelope: unsupported snapshot_schema_version {version}", path=target
        )
    extra = sorted(set(payload) - _WRAPPER_KEYS)
    if extra:
        _fail(f"unknown keys fail closed: {extra}", path=target)

    account = payload.get("account_scope_id")
    if not isinstance(account, str):
        _fail("account_scope_id required", path=target)
    scope = validate_account_scope_id(account)
    epoch_number = payload.get("epoch_number")
    if isinstance(epoch_number, bool) or not isinstance(epoch_number, int) or epoch_number < 0:
        _fail("epoch_number must be a non-negative integer", path=target)
    epoch_id = payload.get("epoch_id")
    if not isinstance(epoch_id, str) or not epoch_id:
        _fail("epoch_id must be a nonempty string", path=target)
    incorporated = payload.get("incorporated_through_seq")
    if isinstance(incorporated, bool) or not isinstance(incorporated, int) or incorporated < 0:
        _fail("incorporated_through_seq must be a non-negative integer", path=target)
    kind = payload.get("publication_kind")
    if kind not in PUBLICATION_KINDS:
        _fail(f"publication_kind must be one of {sorted(PUBLICATION_KINDS)}", path=target)
    timestamp = payload.get("snapshot_timestamp")
    if timestamp is not None and not isinstance(timestamp, str):
        _fail("snapshot_timestamp must be a string or null", path=target)
    return SnapshotEnvelope(
        path=normalize_snapshot_path(target),
        snapshot_timestamp=timestamp,
        nodes_export=dict(nodes_export),
        legacy=False,
        snapshot_schema_version=version,
        account_scope_id=scope,
        epoch_number=epoch_number,
        epoch_id=epoch_id,
        incorporated_through_seq=incorporated,
        publication_kind=kind,
    )


def validate_snapshot_lineage(
    envelope: SnapshotEnvelope,
    *,
    account_scope_id: str,
    epoch_number: int | None = None,
    epoch_id: str | None = None,
    incorporated_through_seq: int | None = None,
    publication_kinds: frozenset[str] | None = None,
) -> SnapshotEnvelope:
    """Fail-closed lineage match of one envelope against expected identity."""

    if envelope.legacy:
        raise CoherenceSchemaError(
            "legacy snapshot wrapper has no lineage to validate", path=envelope.path
        )
    scope = validate_account_scope_id(account_scope_id)
    if envelope.account_scope_id != scope:
        raise CoherenceAccountMismatchError(
            "snapshot envelope account does not match the expected account scope",
            path=envelope.path,
            account_scope_id=envelope.account_scope_id,
        )
    if epoch_number is not None and envelope.epoch_number != epoch_number:
        raise CoherenceSchemaError(
            f"snapshot envelope epoch_number {envelope.epoch_number} != expected {epoch_number}",
            path=envelope.path,
        )
    if epoch_id is not None and envelope.epoch_id != epoch_id:
        raise CoherenceSchemaError(
            "snapshot envelope epoch_id does not match the expected epoch identity",
            path=envelope.path,
        )
    if incorporated_through_seq is not None and (
        envelope.incorporated_through_seq != incorporated_through_seq
    ):
        raise CoherenceSchemaError(
            f"snapshot envelope watermark {envelope.incorporated_through_seq} != "
            f"expected {incorporated_through_seq}",
            path=envelope.path,
        )
    if publication_kinds is not None and envelope.publication_kind not in publication_kinds:
        raise CoherenceSchemaError(
            f"snapshot envelope publication_kind {envelope.publication_kind!r} not in "
            f"{sorted(publication_kinds)}",
            path=envelope.path,
        )
    return envelope


def load_manifest_base_envelope(
    paths: CoherencePaths, manifest: CoherenceManifest
) -> SnapshotEnvelope:
    """Read and validate the manifest-referenced base snapshot (strict)."""

    if manifest.base_snapshot_path is None:
        raise CoherenceStateMissingError(
            "manifest carries no base snapshot (bootstrap state)",
            path=paths.manifest_path,
        )
    envelope = read_snapshot_envelope(manifest.base_snapshot_path)
    validate_snapshot_lineage(
        envelope,
        account_scope_id=manifest.account_scope_id,
        epoch_number=manifest.epoch_number,
        epoch_id=manifest.epoch_id,
        incorporated_through_seq=manifest.base_incorporated_through_seq,
    )
    return envelope


def read_journal_records(
    paths: CoherencePaths, floor: int, head: int
) -> list[JournalTransaction]:
    """Read the contiguous committed record range floor..head (fail-closed)."""

    records: list[JournalTransaction] = []
    for sequence in range(floor, head + 1):
        record_path = paths.journal_dir / f"{sequence:020d}.json"
        if not record_path.exists():
            raise JournalIntegrityError(
                "committed journal record missing during materialization",
                path=record_path,
                sequence=sequence,
            )
        records.append(_read_record_file(record_path, paths))
    return records


# @beacon[
#   id=coherence@materialize-payload-with-records,
#   slice_labels=mci-epoch-snapshot,mci-journal-effects,
#   kind=ast,
#   comment=S2 replay acquires one borrowed indexed workspace before the record loop, applies contiguous authoritative transactions atomically and finishes once to return an owned transferable payload, including empty tails.,
# ]
def materialize_payload_with_records(
    base_nodes_export: Mapping[str, Any],
    records: list[JournalTransaction],
    *,
    start_sequence: int,
    account_scope_id: str | None = None,
    diagnostics: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Replay committed records onto a base payload (replay_authoritative).

    Idempotent canonical after-state replay makes it safe to apply a record
    even when the base already contains that mutation.  Replay remains
    strictly contiguous and sequence-ordered.  ``diagnostics`` (S03) is an
    optional sink for replay-authoritative divergence records; existing
    callers are unaffected. One private indexed workspace survives the whole
    tail; finish transfers its owned payload only after complete success.
    """

    working = EffectWorkingCopy.from_payload(base_nodes_export, ownership="borrowed")
    sequence = start_sequence
    for record in records:
        apply_journal_transaction(
            working,
            record,
            expected_sequence=sequence,
            mode=ApplicationMode.REPLAY_AUTHORITATIVE,
            expected_account=account_scope_id,
            diagnostics=diagnostics,
            input_mode="working_copy",
        )
        sequence += 1
    return working.finish()


@dataclass(frozen=True)
class MaterializedView:
    """One fully materialized current lineage view."""

    payload: dict[str, Any]
    adopted_sequence: int
    base_snapshot_path: str
    snapshot_timestamp: str | None
    epoch_number: int
    epoch_id: str
    incorporated_through_seq: int


# @beacon[
#   id=auto-beacon@materialize_current_payload-eu0e,
#   role=materialize_current_payload,
#   slice_labels=mci-epoch-snapshot,
#   kind=ast,
# ]
def materialize_current_payload(
    paths: CoherencePaths,
    manifest: CoherenceManifest,
    *,
    diagnostics: list[dict[str, Any]] | None = None,
) -> MaterializedView:
    """Materialize the manifest base + active journal through the head.

    ``diagnostics`` (S03) is an optional sink for replay-authoritative
    divergence records emitted while folding the active range.
    """

    envelope = load_manifest_base_envelope(paths, manifest)
    records = read_journal_records(
        paths, manifest.journal_floor_seq, manifest.journal_head_seq
    )
    payload = materialize_payload_with_records(
        envelope.nodes_export,
        records,
        start_sequence=manifest.journal_floor_seq,
        account_scope_id=manifest.account_scope_id,
        diagnostics=diagnostics,
    )
    return MaterializedView(
        payload=payload,
        adopted_sequence=manifest.journal_head_seq,
        base_snapshot_path=envelope.path,
        snapshot_timestamp=envelope.snapshot_timestamp,
        epoch_number=manifest.epoch_number,
        epoch_id=manifest.epoch_id,
        incorporated_through_seq=manifest.base_incorporated_through_seq,
    )


# ---------------------------------------------------------------------------
# Bootstrap-only legacy discovery (S02 §10.1)
# ---------------------------------------------------------------------------


def list_legacy_snapshot_candidates(snapshot_dir: str | os.PathLike[str]) -> list[str]:
    """Newest-first absolute paths of legacy-named snapshot files."""

    directory = Path(snapshot_dir)
    try:
        names = [
            name
            for name in os.listdir(directory)
            if name.endswith(_SNAPSHOT_SUFFIX) and not name.endswith(".tmp")
        ]
    except FileNotFoundError:
        return []
    names.sort(reverse=True)
    return [normalize_snapshot_path(directory / name) for name in names]


def select_legacy_bootstrap_candidate(
    snapshot_dir: str | os.PathLike[str],
) -> SnapshotEnvelope | None:
    """Select the lexicographically newest USABLE legacy wrapper (bootstrap only).

    Newest-first scan; the first GENUINELY LEGACY wrapper that parses and
    carries a usable ``nodes_export`` object wins.  An S02 lineage wrapper
    (any account, including this one) is NEVER a legacy bootstrap candidate —
    adopting one would violate account-scoped isolation and the frozen
    "newest usable legacy wrapper only" rule.  Never raises on an unusable
    candidate — it simply moves to the next older one.
    """

    for candidate in list_legacy_snapshot_candidates(snapshot_dir):
        try:
            envelope = read_snapshot_envelope(candidate, allow_legacy=True)
        except CoherenceError:
            continue
        if not envelope.legacy:
            # An already-lineaged S02 snapshot (this or another account) is
            # not bootstrap material; keep scanning older candidates.
            continue
        return envelope
    return None


# ---------------------------------------------------------------------------
# Manifest-aware GC protected-set computation (S02 §11.2)
# ---------------------------------------------------------------------------


# @beacon[
#   id=auto-beacon@collect_protected_snapshot_paths-40iu,
#   role=collect_protected_snapshot_paths,
#   slice_labels=mci-epoch-snapshot,
#   kind=ast,
#   comment=Protect manifest bases and the immutable active-candidate projection under existing age/count policy; no job-basis or publication-ownership file is read.,
# ]
def collect_protected_snapshot_paths(
    jobs_root: str | os.PathLike[str],
    *,
    active_candidate_paths: frozenset[str],
) -> tuple[set[str], dict[str, Any]]:
    """Compute the set of snapshot paths GC must never delete.

    Protects:
    - every account manifest's referenced active base (regardless of age);
    - active memory candidates supplied as an immutable projection;

    A malformed/unreadable manifest makes the whole computation
    conservative: ``diagnostics["abort"]`` is True and callers must delete
    NOTHING. GC never opens job JSON or edits durable coherence state.
    """

    protected: set[str] = set(active_candidate_paths)
    diagnostics: dict[str, Any] = {
        "abort": False,
        "manifests_read": 0,
        "jobs_read": 0,
        "errors": [],
    }
    root = gov.get_jobs_root(jobs_root)
    coherence_root = root / ".coherence"

    # 1. Every account manifest's active base — STRICT schema validation
    # (A4): parseable-but-invalid artifacts (unknown future versions, unknown
    # keys, account-vs-directory mismatch) abort conservatively exactly like
    # unparseable ones, so a relocated/renamed field can never cause a live
    # base to be garbage-collected.
    from .schema import CoherenceManifest

    if coherence_root.exists():
        for account_dir in sorted(coherence_root.iterdir()):
            if not account_dir.is_dir():
                continue
            manifest_path = account_dir / "manifest.json"
            if not manifest_path.exists():
                continue
            try:
                with manifest_path.open("r", encoding="utf-8") as handle:
                    payload = json.load(handle)
                if not isinstance(payload, Mapping):
                    raise CoherenceSchemaError(
                        "manifest: top-level JSON must be an object", path=manifest_path
                    )
                manifest = CoherenceManifest.from_dict(payload, path=manifest_path)
                if manifest.account_scope_id != account_dir.name:
                    raise CoherenceAccountMismatchError(
                        "manifest account does not match its account directory",
                        path=manifest_path,
                        account_scope_id=manifest.account_scope_id,
                    )
                if manifest.base_snapshot_path:
                    protected.add(normalize_snapshot_path(manifest.base_snapshot_path))
                diagnostics["manifests_read"] += 1
            except Exception as exc:  # noqa: BLE001 — conservative no-delete
                diagnostics["abort"] = True
                diagnostics["errors"].append(f"manifest unreadable: {manifest_path}: {exc}")

    return protected, diagnostics
