"""Reusable fault-injection harness for the coherence program.

S01 §11 scaffolding: importable WITHOUT server startup or credentials; no
real Workflowy, browser, network, or user runtime directories.  Used by
S01's own tests lightly and by S02–S05 (and S08's deterministic Profile C
failover matrix) as the shared fault surface.

CUT-2B: the durable-writer fault actors are TEST-ONLY NAMED SEAMS.  A seam
raises :class:`InjectedFault` after capturing the isolated test root's
relative-path→bytes durable-state image BEFORE the raise
(:class:`SeamCapture`); the invocation unwinds, the successor fixture is
rebuilt from that image with fresh in-memory authority.  No harness seam
calls a process exit or launches a process. The converted S01/S02/S04 fault
suites retain only their ordinary isolated import child; shared child runners
remain for the unchanged singleton-guard/startup and other retained fixtures.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from workflowy_mcp.coherence import (
    CoherencePaths,
    DurableFileOps,
    coherence_paths,
    ensure_coherence_directories,
    initialize_manifest_if_absent,
    new_bootstrap_manifest,
)
from workflowy_mcp.coherence.canonical_node import _dewhiten_text
from workflowy_mcp.coherence.errors import JournalPublicationError

# ---------------------------------------------------------------------------
# Deterministic identities
# ---------------------------------------------------------------------------

ACCOUNT_A = "a" * 32
ACCOUNT_B = "b" * 32

NOW = "2026-11-01T00:00:00+00:00"


def make_account(seed: str = "a") -> str:
    return seed * 32


# ---------------------------------------------------------------------------
# Isolated coherence root
# ---------------------------------------------------------------------------


def make_coherence_root(
    tmp_path: Path, account: str = ACCOUNT_A, *, bootstrap: bool = True
) -> CoherencePaths:
    """Isolated temporary jobs/coherence root (+ optional bootstrap manifest)."""

    jobs_root = tmp_path / "cartographer_jobs"
    jobs_root.mkdir(parents=True, exist_ok=True)
    paths = coherence_paths(account, jobs_root=jobs_root)
    if bootstrap:
        initialize_manifest_if_absent(paths, new_bootstrap_manifest(account, now=NOW))
    else:
        ensure_coherence_directories(paths)
    return paths


def make_base_snapshot(directory: Path, nodes: list[dict[str, Any]], *, name: str = "base.json") -> Path:
    """Deterministic base snapshot file for adoption/replay tests."""

    directory.mkdir(parents=True, exist_ok=True)
    target = directory / name
    with target.open("w", encoding="utf-8") as handle:
        json.dump({"nodes": nodes}, handle, ensure_ascii=False)
    return target


def seed_publication_record(
    paths: CoherencePaths,
    *,
    refresh_kind: str = "system",
    status: str = "claimed",
    cut_token: Any = None,
    basis_head_seq: int | None = None,
    candidate_snapshot_path: str | None = None,
    **overrides: Any,
) -> Any:
    """Seed only the private memory carrier in an isolated fixture root."""
    from workflowy_mcp.coherence.refresh_authority import (
        _PublicationRecord,
        _store_publication_record,
    )
    from workflowy_mcp.coherence.schema import (
        FULL_REFRESH_KINDS,
        RefreshCutToken,
        new_claim_id,
        new_epoch_id,
        read_external_dirt,
        read_manifest,
        utc_now,
    )

    manifest = read_manifest(paths, required=True)
    dirt = read_external_dirt(paths)
    if cut_token is None and refresh_kind in FULL_REFRESH_KINDS:
        cut_token = RefreshCutToken(
            account_scope_id=manifest.account_scope_id,
            epoch_number=manifest.epoch_number,
            epoch_id=manifest.epoch_id,
            journal_head_seq=manifest.journal_head_seq,
            hard_barrier_generation=manifest.dirty_status.generation,
            external_dirt_generation=dirt.generation,
            captured_at=utc_now(),
        )
    fields = dict(
        account_scope_id=manifest.account_scope_id,
        claim_id=new_claim_id(),
        refresh_kind=refresh_kind,
        status=status,
        cut_token=cut_token,
        requested_hard_generation=manifest.dirty_status.generation if manifest.dirty_status.active else 0,
        requested_external_generation=dirt.generation if dirt.active else 0,
        target_epoch_number=manifest.epoch_number + 1,
        target_epoch_id=new_epoch_id(),
        started_at=utc_now(),
        updated_at=utc_now(),
        result=None,
        candidate_snapshot_path=candidate_snapshot_path,
        basis_head_seq=basis_head_seq,
    )
    fields.update(overrides)
    return _store_publication_record(paths, _PublicationRecord(**fields))


def cut2b_neutralize_barrier_advance(monkeypatch) -> None:
    """Test-only counterfactual of the real generation rule, never a product hook."""
    if not os.environ.get("CUT2B_NEUTRALIZE_BARRIER_ADVANCE"):
        return
    import inspect
    from workflowy_mcp.coherence import barrier
    source = inspect.getsource(barrier._raise_global_dirty_barrier_locked)
    old = "generation=active.generation + 1"
    assert old in source
    namespace = dict(vars(barrier))
    exec(compile(source.replace(old, "generation=active.generation + 2", 1), "<cut2b-barrier-advance>", "exec"), namespace)
    monkeypatch.setattr(barrier, "_raise_global_dirty_barrier_locked", namespace["_raise_global_dirty_barrier_locked"])


def publish_fixture_head(client: Any, paths: CoherencePaths) -> Any:
    """Publish an isolated fixture's base/tail without changing its operation pin.

    CUT-2A daemon boundaries capture the local publisher, not another actor's
    manifest.  A non-clearing checkpoint offer keeps dirty/effect evidence.
    """
    from workflowy_mcp.coherence.schema import read_manifest
    from workflowy_mcp.coherence.snapshot_store import materialize_current_payload

    manifest = read_manifest(paths, required=True)
    materialized = materialize_current_payload(paths, manifest)
    return client._publish_lineage_view(
        materialized.payload,
        epoch_number=materialized.epoch_number,
        epoch_id=materialized.epoch_id,
        adopted_sequence=materialized.adopted_sequence,
        snapshot_timestamp=materialized.snapshot_timestamp,
        account_scope_id=manifest.account_scope_id,
        publication_kind="checkpoint",
        ownership="transferred",
    )


# ---------------------------------------------------------------------------
# S03-B operation-boundary client-double surface
# ---------------------------------------------------------------------------

#: Non-sentinel deterministic epoch identity for boundary doubles (the all-zero
#: form is the reserved UNPOPULATED sentinel and must never appear here).
BOUNDARY_DOUBLE_EPOCH_ID = "1" * 32


def make_boundary_capable(
    client: Any,
    *,
    nodes: Iterable[Mapping[str, Any]] = (),
    dirty_ids: Iterable[str] = (),
    boundary_status: Any = None,
) -> Any:
    """Install the S03-B operation-boundary surface on a client double.

    Handler/front tests construct uninitialized ``WorkFlowyClientNexus``
    doubles with a stubbed ``_get_nodes_export_cache_nodes``.  S03-B wraps
    every cache-backed WS/MCP front in ``cache_operation_boundary``, which
    requires the client to provide the published view and welded capture:

    - ``_cache_view_publisher`` — a populated ``CacheViewPublisher`` (the
      boundary pins its view; the pinned snapshot's ``nodes_by_id`` is built
      from ``nodes`` so it matches the test's stubbed cache contents);
    - ``_capture_view_and_dirty()`` — the welded (view, dirty) pair (B1.1).

    S04 adds boundary status:

    - ``_coherence_boundary_status()`` — the §7.6 boundary-status capture
      hook.  The REAL method lives on ``WorkFlowyClientCore`` and therefore
      EXISTS on uninitialized doubles, where it would dereference missing
      governor state — so the double ALWAYS installs this stand-in shadow
      (the same pattern as the capture hook).  Default None (no
      advisory); pass a ``CoherenceBoundaryStatus`` to exercise advisory
      paths.

    ``dirty_ids`` seeds the welded dirty bookkeeping.  Returns the client.
    """

    from workflowy_mcp.coherence.cache_view import CacheView, CacheViewPublisher

    view = CacheView.build(
        epoch_number=0,
        epoch_id=BOUNDARY_DOUBLE_EPOCH_ID,
        adopted_sequence=0,
        nodes_export={"nodes": [dict(node) for node in nodes]},
        account_scope_id=None,
    )
    publisher = CacheViewPublisher(view)
    welded_dirty = frozenset(dirty_ids)
    client._cache_view_publisher = publisher

    client._capture_view_and_dirty = lambda: (publisher.capture(), welded_dirty)
    client._coherence_boundary_status = lambda: boundary_status
    return client


# ---------------------------------------------------------------------------
# Subprocess launcher and cross-process synchronization
# ---------------------------------------------------------------------------


def run_subprocess(
    code: str,
    *args: str,
    env: Mapping[str, str] | None = None,
    timeout: float = 30.0,
) -> subprocess.CompletedProcess[str]:
    """Launch a Python subprocess with the ACTIVE interpreter."""

    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    return subprocess.run(
        [sys.executable, "-c", code, *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=full_env,
    )


def launch_subprocess(
    code: str,
    *args: str,
    env: Mapping[str, str] | None = None,
) -> subprocess.Popen[str]:
    """Launch a long-running Python subprocess with the ACTIVE interpreter."""

    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    return subprocess.Popen(
        [sys.executable, "-c", code, *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=full_env,
    )


def wait_for_file(path: Path, timeout: float = 10.0, poll: float = 0.02) -> bool:
    """Bounded wait for a sentinel file to appear (cross-process barrier)."""

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(poll)
    return False


def touch(path: Path, text: str = "1") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# Durable-writer fault points (S01 §11 named minimum set)
# ---------------------------------------------------------------------------

FAULT_POINTS = (
    "before_record_temp_write",
    "after_record_file_fsync",
    "before_record_replace",
    "after_record_replace_before_manifest",
    "before_manifest_temp_write",
    "after_manifest_file_fsync",
    "before_manifest_replace",
    "after_manifest_replace",
)


@dataclass(frozen=True)
class FaultImage:
    """One isolated test root's durable state, captured AT a named seam.

    ``files`` maps POSIX relative paths (from the fixture root) to exact
    bytes.  A successor fixture is reconstructed from this pre-raise image;
    never from the files an exception's own cleanup left behind.
    """

    point: str
    root: Path
    files: dict[str, bytes]


def snapshot_fault_image(root: str | Path, *, point: str = "") -> FaultImage:
    """Capture an isolated test root's relative-path→bytes durable image."""

    base = Path(root)
    files = {
        entry.relative_to(base).as_posix(): entry.read_bytes()
        for entry in sorted(base.rglob("*"))
        if entry.is_file()
    }
    return FaultImage(point=point, root=base, files=files)


class SeamCapture:
    """Test-only slot holding the image a named seam captured before raising."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.images: list[FaultImage] = []

    def capture(self, point: str) -> FaultImage:
        image = snapshot_fault_image(self.root, point=point)
        self.images.append(image)
        return image

    @property
    def image(self) -> FaultImage:
        assert self.images, "no named seam tripped; no pre-raise image captured"
        return self.images[0]

    def restart_image(self) -> FaultImage:
        """The death image; the wrong-image probe substitutes POST-unwind bytes.

        ``CUT2B_NEUTRALIZE_FAULT_IMAGE`` is the spec's test-only counterfactual
        proving a fault exception is not asserted equivalent to hard death
        without the pre-unwind observation: wherever exception cleanup or a
        sealer's follow-up barrier changed bytes, the post-unwind image must
        fail the test's durable-state matrix.
        """

        image = self.image
        if os.environ.get("CUT2B_NEUTRALIZE_FAULT_IMAGE"):
            return snapshot_fault_image(self.root, point=image.point)
        return image


def restore_fault_image(root: str | Path, image: FaultImage) -> Path:
    """Restore captured bytes; reuse the origin to preserve absolute base paths."""

    target = Path(root)
    if target.exists() and any(target.iterdir()):
        if target != image.root:
            raise ValueError(f"successor root is not empty: {target}")
        # The old invocation has joined before this test-only reset. Restore
        # the SAME isolated root: manifest absolute base paths must resolve
        # to captured bytes, never to the abandoned post-unwind fixture.
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=True)
    for relative, data in image.files.items():
        path = target / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return target


def restore_successor(
    image: FaultImage,
    destination: str | Path,
    account: str = ACCOUNT_A,
) -> CoherencePaths:
    """Rebuild the isolated fixture over a captured image (fresh memory)."""

    root = restore_fault_image(destination, image)
    return make_coherence_root(root, account, bootstrap=False)


def reset_fixture_memory(
    root: str | Path, *, accounts: Iterable[str] = ()
) -> None:
    """Drop ONLY this fixture root's in-memory authority carriers."""

    from workflowy_mcp import carto_job_governance as gov
    from workflowy_mcp.coherence import checkpoint as checkpoint_module
    from workflowy_mcp.coherence import paths as lock_paths
    from workflowy_mcp.coherence import refresh_authority as ra

    prefix = os.path.normcase(os.path.abspath(os.fspath(root))) + os.sep

    def belongs(key: object) -> bool:
        text = os.path.normcase(os.path.abspath(os.fspath(key)))
        return text == prefix[:-1] or text.startswith(prefix)

    for registry in (
        ra._PUBLICATION_RECORDS,
        lock_paths._COHERENCE_LOCKS,
        gov._ADMISSION_LOCKS,
    ):
        for key in tuple(registry):
            if isinstance(key, str) and belongs(key):
                if registry is not ra._PUBLICATION_RECORDS:
                    assert not registry[key].locked(), "old fixture lock still held at restart"
                registry.pop(key, None)
    for account in accounts:
        task = checkpoint_module._CHECKPOINT_TASKS.get(account)
        assert task is None or task.done(), "old fixture checkpoint task has not joined"
        checkpoint_module._CHECKPOINT_TASKS.pop(account, None)


class InjectedFault(JournalPublicationError):
    """Raised by the harness's injected durable-writer fault seams.

    A JournalPublicationError subclass so the durable writer re-raises it
    UNWRAPPED (its own publication-failure type), letting fault tests
    assert the exact injected seam.  This is a NAMED SEAM EXCEPTION, never
    a process exit: the fixture's durable image is captured before the
    raise and its successor is reconstructed from that image.
    """

    def __init__(self, point: str) -> None:
        self.point = point
        super().__init__(f"injected fault at {point}")


class _BoundaryOps:
    """Wrap the durable writer's seams so a named boundary raises.

    The two artifact classes are distinguished by filename: the record temp
    file targets the journal directory (20-digit sequence name); the
    manifest temp targets ``manifest.json``.
    """

    def __init__(
        self,
        fail_at: str | None,
        *,
        times: int | None = None,
        capture: SeamCapture | None = None,
    ) -> None:
        if fail_at is not None and fail_at not in FAULT_POINTS:
            raise ValueError(f"unknown fault point {fail_at!r}")
        self.fail_at = fail_at
        self.times = None if times is None else max(1, int(times))
        self.capture = capture
        self._seen = 0

    def _trip(self, point: str) -> None:
        if point != self.fail_at:
            return
        if os.environ.get("CUT2B_NEUTRALIZE_FAULT_SEAM"):
            return  # test-only probe: the selected named seam never fires
        if self.times is not None:
            self._seen += 1
            if self._seen != self.times:
                return  # exit_ops fires once; fault_ops retains repeated faults
        if self.capture is not None:
            self.capture.capture(point)
        raise InjectedFault(point)

    @staticmethod
    def _is_record_tmp(path: Path) -> bool:
        # Structural discrimination (F-5): record artifacts (and their temp
        # files) live under the journal directory — robust to temp-naming
        # changes; the manifest and its temp live in the account root.
        return path.parent.name == "journal"

    def ops(self) -> DurableFileOps:
        from workflowy_mcp.carto_job_governance import (
            _default_mkdir,
            _default_replace,
            _default_tmp_name,
            _default_unlink,
            _default_write_fsync,
        )

        def write_fsync(path: Path, text: str) -> None:
            is_record = self._is_record_tmp(path)
            if is_record:
                self._trip("before_record_temp_write")
            else:
                self._trip("before_manifest_temp_write")
            _default_write_fsync(path, text)
            if is_record:
                self._trip("after_record_file_fsync")
            else:
                self._trip("after_manifest_file_fsync")

        def replace(tmp: Path, target: Path) -> None:
            is_record = self._is_record_tmp(tmp)
            if is_record:
                self._trip("before_record_replace")
                _default_replace(tmp, target)
                self._trip("after_record_replace_before_manifest")
            else:
                self._trip("before_manifest_replace")
                _default_replace(tmp, target)
                self._trip("after_manifest_replace")

        return DurableFileOps(
            write_fsync=write_fsync,
            replace=replace,
            mkdir=_default_mkdir,
            unlink=_default_unlink,
            tmp_name=_default_tmp_name,
        )


def fault_ops(fail_at: str | None) -> DurableFileOps:
    """Durable-writer seams that raise :class:`InjectedFault` at ``fail_at``."""

    return _BoundaryOps(fail_at).ops()


def exit_ops(
    fail_at: str,
    *,
    times: int = 1,
    capture: SeamCapture | None = None,
) -> DurableFileOps:
    """Durable-writer seams that raise :class:`InjectedFault` at ``fail_at``.

    CUT-2B: a test-only named seam, NOT a process exit.  The optional
    ``capture`` slot records the fixture's relative-path→bytes durable image
    BEFORE the raise, so the restart fixture is built from the death state.
    """

    return _BoundaryOps(fail_at, times=times, capture=capture).ops()


# ---------------------------------------------------------------------------
# Corrupt-artifact injectors
# ---------------------------------------------------------------------------


def write_malformed(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")


def write_unknown_version(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = dict(payload)
    doc["schema_version"] = 99
    path.write_text(json.dumps(doc), encoding="utf-8")


def write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


# ---------------------------------------------------------------------------
# Atomic-visibility reader loop
# ---------------------------------------------------------------------------


@dataclass
class ReaderResult:
    reads: int = 0
    torn: int = 0
    unexpected: list[Any] = field(default_factory=list)


def run_reader_loop(
    path: Path,
    stop: threading.Event,
    *,
    acceptable: Iterable[Mapping[str, Any]] | None = None,
    poll: float = 0.001,
) -> ReaderResult:
    """Read a JSON artifact in a loop; prove old-or-new atomic visibility.

    Any JSON decode failure (torn read) increments ``torn``; any parsed
    payload outside the acceptable set is recorded.
    """

    result = ReaderResult()
    acceptable_set = None
    if acceptable is not None:
        acceptable_set = {
            json.dumps(dict(item), sort_keys=True, separators=(",", ":")) for item in acceptable
        }
    while not stop.is_set():
        try:
            with path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
            result.reads += 1
            if acceptable_set is not None:
                key = json.dumps(payload, sort_keys=True, separators=(",", ":"))
                if key not in acceptable_set:
                    result.unexpected.append(payload)
        except FileNotFoundError:
            pass
        except PermissionError:
            # Windows transient sharing denial while the writer's atomic
            # os.replace is in flight — skip, never a torn read.
            pass
        except json.JSONDecodeError:
            result.torn += 1
        time.sleep(poll)
    return result


# ---------------------------------------------------------------------------
# Semantic cache comparison (normalizes the documented W1 entity edge)
# ---------------------------------------------------------------------------


def semantic_text_equal(a: Any, b: Any) -> bool:
    """String equality with the DOCUMENTED TOLERATED EDGE normalized: on
    literal-entity content the cache (full-export cascading decode) and a
    live-GET after-state (non-cascading decode) may legitimately differ
    (``<`` vs ``&lt;``) — a cosmetic, refresh-healed cache edge."""

    if not isinstance(a, str) or not isinstance(b, str):
        return a == b
    if a == b:
        return True
    return _dewhiten_text(a) == _dewhiten_text(b)


def assert_semantic_cache_equal(
    actual: Mapping[str, Any],
    expected: Mapping[str, Any],
    *,
    ignore_keys: Iterable[str] = (),
) -> None:
    """Compare two nodes-export payloads semantically.

    Ignores only explicitly named diagnostic metadata keys; uses
    :func:`semantic_text_equal` for string values so the documented W1
    nested-entity edge cannot spuriously diverge (forwarded to Waypoint B).
    """

    ignored = set(ignore_keys)

    def index(payload: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
        return {str(n["id"]): n for n in payload.get("nodes", []) if isinstance(n, Mapping)}

    a_nodes = index(actual)
    b_nodes = index(expected)
    assert set(a_nodes) == set(b_nodes), (
        f"node id sets differ: only-actual={sorted(set(a_nodes) - set(b_nodes))} "
        f"only-expected={sorted(set(b_nodes) - set(a_nodes))}"
    )
    for node_id, a_node in a_nodes.items():
        b_node = b_nodes[node_id]
        keys = (set(a_node) | set(b_node)) - ignored
        for key in sorted(keys):
            a_val = a_node.get(key)
            b_val = b_node.get(key)
            if isinstance(a_val, str) or isinstance(b_val, str):
                assert semantic_text_equal(a_val, b_val), (
                    f"node {node_id} field {key}: {a_val!r} != {b_val!r}"
                )
            else:
                assert a_val == b_val, f"node {node_id} field {key}: {a_val!r} != {b_val!r}"


# ---------------------------------------------------------------------------
# Injectable transport-outcome seam (S01 §11 — the S08 Profile C seam)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# S02 helpers — refresh-authority fixtures
# ---------------------------------------------------------------------------


def s02_make_export(nodes: list[dict[str, Any]]) -> dict[str, Any]:
    """A raw wire-shaped /nodes-export payload for injected fetches."""

    return {"nodes": nodes, "_total_fetched_from_api": len(nodes)}


def s02_fetch(payload: Mapping[str, Any], calls: list[int] | None = None):
    """A counted async fetch returning a fixed payload (S02 authority seam)."""

    async def _fetch() -> Mapping[str, Any]:
        if calls is not None:
            calls.append(1)
        return payload

    return _fetch


class RecordedSleep:
    """An injected sleep that records durations instead of waiting.

    Still yields one real loop tick (``asyncio.sleep(0)``) so concurrent
    tasks and timers are serviced — an awaited coroutine without a real
    suspension point would starve the event loop in wait/poll loops.
    """

    def __init__(self) -> None:
        self.durations: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.durations.append(float(seconds))
        await asyncio.sleep(0)


def s02_set_barrier(paths, generation: int, *, active: bool = True):
    """Inject a synthetic hard barrier into the manifest (S04 owns writes)."""

    from workflowy_mcp.coherence import (
        DirtyBarrierState,
        manifest_with_updates,
        read_manifest,
        write_manifest,
    )

    manifest = read_manifest(paths, required=True)
    barrier = manifest.dirty_status
    new_barrier = DirtyBarrierState(
        schema_version=1,
        account_scope_id=barrier.account_scope_id,
        active=active,
        generation=generation,
        reason_code="synthetic_test" if active else None,
        detail=None,
        transaction_id=None,
        affected_region=None,
        raised_at=NOW if active else None,
    )
    return write_manifest(
        paths,
        manifest_with_updates(manifest, dirty_status=new_barrier),
        expected_epoch_id=manifest.epoch_id,
        expected_head_seq=manifest.journal_head_seq,
    )


def s02_set_external_dirt(paths, generation: int, *, active: bool = True):
    """Inject synthetic external dirt (S16 owns production writes)."""

    from workflowy_mcp.coherence import ExternalDirtState, write_external_dirt

    return write_external_dirt(
        paths,
        ExternalDirtState(
            schema_version=1,
            account_scope_id=paths.account_root.name,
            active=active,
            generation=generation,
            observed_at=NOW if active else None,
            mutation_kind="synthetic" if active else None,
            node_uuid=None,
            parent_uuid=None,
            detail=None,
        ),
    )


def s02_append(paths, *, cache_by_id=None, operation_kind: str = "test"):
    """Append one trivial verified transaction (S02 journal fixtures)."""

    from workflowy_mcp.coherence import (
        JournalProducer,
        SemanticEffect,
        append_journal_transaction,
    )

    return append_journal_transaction(
        paths,
        operation_kind=operation_kind,
        effects=[SemanticEffect.tombstone(["s02-ghost"])],
        producer=JournalProducer(
            pid=os.getpid(), pid_create_time=1.0, owner_kind="process", ownership_kind="none"
        ),
        cache_by_id=cache_by_id,
    )


def s02_node(node_id: str, *, name: str | None = None, **extra: Any) -> dict[str, Any]:
    """One minimal canonical node fixture."""

    node: dict[str, Any] = {
        "id": node_id,
        "name": name if name is not None else f"node-{node_id}",
        "note": "",
        "parent_id": None,
        "priority": 100,
    }
    node.update(extra)
    return node


# ---------------------------------------------------------------------------
# S02 artifact-classified fault seams (the §14.3 crash matrix)
# ---------------------------------------------------------------------------

S02_ARTIFACTS = ("record", "manifest", "dirt", "candidate")
S02_PHASES = ("temp_write", "fsynced", "replace", "unlink")
S02_FAULT_POINTS = tuple(
    f"{artifact}_{phase}" for artifact in S02_ARTIFACTS for phase in S02_PHASES
)


def _s02_artifact_of(path: Path) -> str:
    """Classify one durable-writer path (temp or target) by retained artifact."""

    name = path.name
    if path.parent.name == "journal" or name.startswith(".0"):
        return "record"
    if "external_dirt.json" in name:
        return "dirt"
    if "manifest.json" in name:
        return "manifest"
    if "_nodes_export_snapshot.json" in name:
        return "candidate"
    return "other"


class _S02BoundaryOps:
    """Raise at one artifact-classified durable boundary (test-only seam)."""

    def __init__(
        self,
        fail_at: str | None,
        *,
        times: int = 1,
        capture: SeamCapture | None = None,
    ) -> None:
        if fail_at is not None and fail_at not in S02_FAULT_POINTS:
            raise ValueError(f"unknown S02 fault point {fail_at!r}")
        self.fail_at = fail_at
        self.times = max(1, int(times))
        self.capture = capture
        self._seen = 0

    def _trip(self, point: str) -> None:
        if point != self.fail_at:
            return
        if os.environ.get("CUT2B_NEUTRALIZE_FAULT_SEAM"):
            return  # test-only probe: the selected named seam never fires
        self._seen += 1
        if self._seen != self.times:
            return  # fire exactly ONCE at the Nth matching seam
        if self.capture is not None:
            self.capture.capture(point)
        raise InjectedFault(point)

    def ops(self) -> DurableFileOps:
        from workflowy_mcp.carto_job_governance import (
            _default_mkdir,
            _default_replace,
            _default_tmp_name,
            _default_unlink,
            _default_write_fsync,
        )

        def write_fsync(path: Path, text: str) -> None:
            artifact = _s02_artifact_of(path)
            self._trip(f"{artifact}_temp_write")
            _default_write_fsync(path, text)
            self._trip(f"{artifact}_fsynced")

        def replace(tmp: Path, target: Path) -> None:
            artifact = _s02_artifact_of(target)
            self._trip(f"{artifact}_replace")
            _default_replace(tmp, target)

        def unlink(path: Path) -> None:
            self._trip(f"{_s02_artifact_of(path)}_unlink")
            _default_unlink(path)

        return DurableFileOps(
            write_fsync=write_fsync,
            replace=replace,
            mkdir=_default_mkdir,
            unlink=unlink,
            tmp_name=_default_tmp_name,
        )


def s02_fault_ops(fail_at: str | None, *, times: int = 1) -> DurableFileOps:
    """Artifact-classified raising fault seams for S02 refresh tests."""

    return _S02BoundaryOps(fail_at, times=times).ops()


def s02_exit_ops(
    fail_at: str,
    *,
    times: int = 1,
    capture: SeamCapture | None = None,
) -> DurableFileOps:
    """Artifact-classified seams that raise :class:`InjectedFault` at a seam.

    CUT-2B: a test-only named seam, NOT a process exit.  The vocabulary stays
    ``{record, manifest, dirt, candidate} × {temp_write, fsynced, replace,
    unlink}``; ``capture`` records the isolated root's durable image BEFORE
    the raise.
    """

    return _S02BoundaryOps(fail_at, times=times, capture=capture).ops()


# ---------------------------------------------------------------------------
# Injectable transport-outcome seam (S01 §11 — the S08 Profile C seam)
# ---------------------------------------------------------------------------

TRANSPORT_SUCCESS = "success"
TRANSPORT_TIMEOUT_COMMITTED = "timeout_committed"
TRANSPORT_TIMEOUT_NOT_COMMITTED = "timeout_not_committed"
TRANSPORT_AMBIGUOUS = "ambiguous"
TRANSPORT_SCHEMA_DRIFT = "schema_drift"

TRANSPORT_OUTCOME_KINDS = (
    TRANSPORT_SUCCESS,
    TRANSPORT_TIMEOUT_COMMITTED,
    TRANSPORT_TIMEOUT_NOT_COMMITTED,
    TRANSPORT_AMBIGUOUS,
    TRANSPORT_SCHEMA_DRIFT,
)


@dataclass(frozen=True)
class TransportOutcome:
    """One structured mocked transport outcome.

    S01 uses this seam only lightly; it exists so S08's deterministic
    Profile C failover matrix never has to retrofit the harness.
    """

    kind: str
    payload: Mapping[str, Any] | None = None
    detail: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in TRANSPORT_OUTCOME_KINDS:
            raise ValueError(f"unknown transport outcome kind {self.kind!r}")


class TransportScript:
    """A scripted queue of transport outcomes for injectable transports."""

    def __init__(self, outcomes: Iterable[TransportOutcome]) -> None:
        self._outcomes = list(outcomes)
        self.calls: list[Mapping[str, Any] | None] = []

    def __call__(self, request: Mapping[str, Any] | None = None) -> TransportOutcome:
        self.calls.append(request)
        if not self._outcomes:
            raise AssertionError("TransportScript exhausted")
        return self._outcomes.pop(0)


# ---------------------------------------------------------------------------
# S04 helpers — barrier / checkpoint / boundary-status fixtures
# ---------------------------------------------------------------------------


def s04_checkpoint_config(
    *,
    record_threshold: int = 500,
    byte_threshold: int = 67_108_864,
    warning_bytes: int = 8_388_608,
    max_catchup_passes: int = 3,
) -> Any:
    """A duck-typed CoherenceConfiguration for checkpoint tests (tiny defaults)."""

    from types import SimpleNamespace

    return SimpleNamespace(
        checkpoint_record_threshold=record_threshold,
        checkpoint_byte_threshold=byte_threshold,
        journal_record_warning_bytes=warning_bytes,
        checkpoint_max_catchup_passes=max_catchup_passes,
    )


def s04_boundary_status(
    *,
    hard_dirty: bool = False,
    hard_generation: int = 0,
    hard_reason_code: str | None = None,
    hard_transaction_id: str | None = None,
    external_dirty: bool = False,
    external_generation: int = 0,
    external_mutation_kind: str | None = None,
    epoch_id: str = "e" * 32,
    journal_head_seq: int = 0,
) -> Any:
    """One CoherenceBoundaryStatus fixture (hard-dirty off by default)."""

    from workflowy_mcp.coherence.barrier import CoherenceBoundaryStatus

    return CoherenceBoundaryStatus(
        epoch_id=epoch_id,
        journal_head_seq=journal_head_seq,
        hard_dirty=hard_dirty,
        hard_generation=hard_generation,
        hard_reason_code=hard_reason_code,
        hard_transaction_id=hard_transaction_id,
        external_dirty=external_dirty,
        external_generation=external_generation,
        external_mutation_kind=external_mutation_kind,
    )


def s04_producer() -> Any:
    """One deterministic JournalProducer fixture."""

    from workflowy_mcp.coherence import JournalProducer

    return JournalProducer(
        pid=os.getpid(), pid_create_time=1.0, owner_kind="process", ownership_kind="none"
    )


def s04_append(
    paths,
    *,
    operation_kind: str = "test",
    effects: Any = None,
    transaction_id: str | None = None,
    fs_ops: Any = None,
    checkpoint_request: Any = None,
):
    """Seal one trivial verified transaction via the S04 sealer."""

    from workflowy_mcp.coherence import SemanticEffect, seal_verified_journal_transaction

    if effects is None:
        effects = [SemanticEffect.tombstone(["s04-ghost"])]
    return seal_verified_journal_transaction(
        paths,
        operation_kind=operation_kind,
        effects=effects,
        producer=s04_producer(),
        transaction_id=transaction_id,
        fs_ops=fs_ops,
        checkpoint_request=checkpoint_request,
    )


def s04_append_n(paths, count: int, *, operation_kind: str = "test") -> list:
    """Seal N trivial verified transactions (checkpoint fixtures)."""

    from workflowy_mcp.coherence import SemanticEffect

    out = []
    for index in range(count):
        out.append(
            s04_append(
                paths,
                operation_kind=operation_kind,
                effects=[SemanticEffect.tombstone([f"s04-ghost-{index}"])],
            )
        )
    return out


def s04_write_base_snapshot(paths, snapshot_dir, nodes, *, epoch_number: int, epoch_id: str, incorporated: int = 0) -> str:
    """Serialize a durable full_refresh lineage base candidate (test fixture)."""

    from workflowy_mcp.coherence import new_snapshot_candidate_path, write_snapshot_candidate

    return write_snapshot_candidate(
        new_snapshot_candidate_path(snapshot_dir),
        {"nodes": nodes},
        account_scope_id=paths.account_root.name,
        epoch_number=epoch_number,
        epoch_id=epoch_id,
        incorporated_through_seq=incorporated,
        publication_kind="full_refresh",
    )


def s04_install_base_manifest(
    paths,
    *,
    base_snapshot_path: str,
    epoch_number: int = 0,
    epoch_id: str | None = None,
    incorporated: int = 0,
    journal_head: int = 0,
):
    """Replace the bootstrap manifest with a transition manifest naming a base.

    Epoch 0 + transition + base snapshot is the bootstrap-from-legacy shape
    (no previous_epoch_id); epoch >= 1 carries a durable predecessor.
    """

    from workflowy_mcp.coherence import (
        CoherenceManifest,
        DirtyBarrierState,
        new_epoch_id,
        read_manifest,
        write_manifest,
    )

    account = paths.account_root.name
    current = read_manifest(paths, required=True)
    manifest = CoherenceManifest(
        schema_version=1,
        account_scope_id=account,
        epoch_number=epoch_number,
        epoch_id=epoch_id or new_epoch_id(),
        base_snapshot_path=str(base_snapshot_path),
        base_incorporated_through_seq=incorporated,
        journal_floor_seq=incorporated + 1,
        journal_head_seq=journal_head,
        coherence_mode="transition",
        dirty_status=DirtyBarrierState.clean(account),
        created_at=NOW,
        updated_at=NOW,
        previous_epoch_id=("p" * 32) if epoch_number >= 1 else None,
    )
    return write_manifest(paths, manifest, expected_epoch_id=current.epoch_id)


def s04_raise_barrier(
    paths,
    *,
    reason_code: str = "journal_publication_failed",
    transaction_id: str | None = "tx-s04",
    affected_region: dict | None = None,
    detail: str | None = None,
    fs_ops: Any = None,
):
    """Thin fixture wrapper over the S04 barrier authority."""

    from workflowy_mcp.coherence import raise_global_dirty_barrier

    return raise_global_dirty_barrier(
        paths,
        reason_code=reason_code,
        transaction_id=transaction_id,
        affected_region=affected_region,
        detail=detail,
        fs_ops=fs_ops,
    )
