"""S04 §12.1/§12.2 — fault completion over TEST-ONLY named durable seams.

Every former process-death point is a named seam that captures the isolated
root's relative-path→bytes durable image BEFORE raising
:class:`InjectedFault`; the invocation unwinds and the successor fixture is
rebuilt from that image with fresh in-memory authority. No subprocess or
process exit participates, and the post-unwind files are never asserted as
hard death.  Shipped post-publish order is claim-terminal (memory-only)
BEFORE journal retirement (S04 Remediation Chain 1, deviation 1: both orders
satisfy the invariants — post-publish steps are independent; residue is
harmless and retried).

§11.6 death point              → seam / test coverage → expected lineage
───────────────────────────────────────────────────────────────────────────
after checkpoint claim         → basis_capture (state-equivalent: no durable
  before/after basis capture     mutation between claim acquisition and the
  during private replay          basis-capture lock) + private_replay —
                                 test_kill_during_basis_capture_or_replay:
                                 OLD epoch wholly authoritative; dead claim
                                 memory discarded; successor publishes E+1.
after candidate fsync          → candidate_fsynced seam (post-temp-fsync/
  (§12.2 after_checkpoint_       pre-replace) in test_kill_before_epoch_
   candidate_fsync)              publish_leaves_old_lineage
                                 [after_candidate_fsync]: OLD epoch; durable
                                 temp litter harmless.
before candidate replace       → candidate_replace seam (the honest meaning)
  (§12.2 before_checkpoint_      [before_candidate_replace — the RENAMED id;
   candidate_replace)            it previously claimed 'after' while tripping
                                 BEFORE the replace]: OLD epoch.
after candidate replace        → manifest_temp_write seam (candidate durable
  (§12.2 after_checkpoint_       at its final path; manifest publish not yet
   candidate_replace)            durable) [after_candidate_replace]: OLD
                                 epoch; unreferenced candidate residue
                                 harmless/GC-eligible; no torn candidate.
before epoch revalidation      → state-equivalent to after_candidate_replace
  (§12.2 before_checkpoint_      (no durable transition between the candidate
   epoch_revalidate)             replace and the revalidation lock): covered
                                 by the same test.  OLD epoch.
after tail capture             → same pre-publish state class (tail capture
  (§12.2 after_checkpoint_       fires only on head advancement under the
   tail_capture)                 catch-up lock; no durable mutation):
                                 covered by the after_candidate_replace
                                 equivalence.  OLD epoch.
before epoch publish           → manifest_replace seam [before_epoch_
  (§12.2 before_checkpoint_      publish]: OLD epoch.
   epoch_publish)
after epoch publish            → no durable seam: the terminal claim status is
  (§12.2 after_checkpoint_       in-memory only.  Publication is the epoch's
   epoch_publish; §11.6          whole durable boundary; the captured image
   'before claim terminal        holds the NEW epoch.  A successor rotation
   update')                      sees it as authoritative.
before journal retirement      → record_unlink seam, times=1 (first unlink
  (§12.2 before_journal_         attempt = retirement start; durable state
   retirement)                   identical to the pre-retirement window) in
                                 test_kill_during_journal_retirement_leaves_
                                 retryable_residue: NEW epoch; zero
                                 deletions; next maintenance retires.
mid-retirement                 → record_unlink seam, times=2 (first journal
  (§12.2 during_journal_         unlink LANDS, the seam fires at the second) in
   retirement)                   test_kill_mid_retirement_partial_residue:
                                 NEW epoch; PARTIAL residue; next
                                 maintenance retires it; the active tail
                                 (seq >= floor) is NEVER deleted.
§12.2 after_retirement_        → UNREACHABLE in the shipped order (terminal
before_claim_terminal            status precedes retirement — Chain-1
                                 deviation 1); both orders satisfy the
                                 invariants.  No seam exists to dishonestly
                                 name after it.

Every test asserts from the PRE-RAISE image: old-or-new epoch wholly
authoritative (never a half epoch), no torn manifest (strict read), no torn
candidate (every envelope on disk parses), no active-tail deletion, and the
successor fixture recovering with fresh memory.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
from pathlib import Path

import pytest
from coherence_harness import (
    ACCOUNT_A,
    InjectedFault,
    SeamCapture,
    make_coherence_root,
    reset_fixture_memory,
    restore_successor,
    run_reader_loop,
    s02_exit_ops,
    s02_node,
    s04_append,
    s04_append_n,
    s04_checkpoint_config,
    s04_install_base_manifest,
    s04_producer,
    s04_raise_barrier,
    s04_write_base_snapshot,
)

from workflowy_mcp.coherence import (
    SemanticEffect,
    append_journal_transaction,
    parse_journal_record_filename,
    read_manifest,
    run_checkpoint_rotation,
)

_KILL_CHECKPOINT_SEAMS = [
    "candidate_temp_write",
    "candidate_fsynced",
    "candidate_replace",
    "manifest_temp_write",
    "manifest_replace",
]

#: ``seam -> (candidate temp durable, manifest temp durable)`` at the
#: PRE-RAISE instant.  The atomic writer's ``finally`` unlinks its own temp on
#: unwind, so these are the artifacts that distinguish the captured death
#: image from the post-unwind files.
_KILL_TEMP_EXPECTATIONS = {
    "candidate_temp_write": (False, False),
    "candidate_fsynced": (True, False),
    "candidate_replace": (True, False),
    "manifest_temp_write": (False, False),
    "manifest_replace": (False, True),
}


def _snapshot_dir(root: Path) -> Path:
    return root / "nodes_export_cache"


def _based_manifest(paths, root: Path) -> None:
    base = s04_write_base_snapshot(
        paths, _snapshot_dir(root), [s02_node("a")], epoch_number=0, epoch_id="b" * 32
    )
    s04_install_base_manifest(paths, base_snapshot_path=base, epoch_id="b" * 32)


def _kill_fixture(root: Path):
    """Epoch-0 based fixture with three active records for a seam test."""

    paths = make_coherence_root(root, ACCOUNT_A)
    _based_manifest(paths, root)
    s04_append_n(paths, 3)
    return paths


def _journal_sequences(paths) -> list[int]:
    if not paths.journal_dir.exists():
        return []
    return sorted(
        seq
        for entry in paths.journal_dir.iterdir()
        if (seq := parse_journal_record_filename(entry.name)) is not None
    )


def _assert_snapshot_envelopes_parse(root: Path) -> None:
    """No torn candidate: every snapshot envelope on disk parses as JSON."""
    for entry in _snapshot_dir(root).glob("*_nodes_export_snapshot.json"):
        json.loads(entry.read_text(encoding="utf-8"))


def _rotate(paths, root: Path, *, threshold: int = 2):
    return asyncio.run(
        run_checkpoint_rotation(
            paths,
            snapshot_dir=_snapshot_dir(root),
            config=s04_checkpoint_config(record_threshold=threshold),
        )
    )


# ---------------------------------------------------------------------------
# §12.2 checkpoint fault matrix
# ---------------------------------------------------------------------------


def _restart_after_seam(tmp_path, capture: SeamCapture, root: Path, *, image=None):
    """Explicit seam+image assertion, then a fresh-memory successor fixture."""

    assert capture.images, "the selected named seam never tripped (no image)"
    reset_fixture_memory(root, accounts=(ACCOUNT_A,))
    selected = capture.restart_image() if image is None else image
    successor_root = root
    paths = restore_successor(selected, successor_root, ACCOUNT_A)
    reset_fixture_memory(successor_root, accounts=(ACCOUNT_A,))
    return paths, successor_root


@pytest.mark.parametrize(
    "seam",
    _KILL_CHECKPOINT_SEAMS,
    ids=[
        "before_candidate_write",
        "after_candidate_fsync",
        "before_candidate_replace",
        "after_candidate_replace",
        "before_epoch_publish",
    ],
)
def test_kill_before_epoch_publish_leaves_old_lineage(tmp_path, seam):
    """Faults from the first candidate touch through the manifest publish
    attempt: the OLD epoch is wholly authoritative in the PRE-RAISE image.
    Honest id↔seam map (S04 Remediation Chain 3): ``candidate_replace`` trips
    BEFORE the replace (id ``before_candidate_replace`` — previously
    mislabeled 'after'); the true post-candidate-replace/pre-publication
    point is the ``manifest_temp_write`` seam (id
    ``after_candidate_replace``); ``candidate_fsynced`` is the true
    post-temp-fsync/pre-replace point."""
    root = tmp_path / "death"
    root.mkdir()
    paths = _kill_fixture(root)
    capture = SeamCapture(root)
    asyncio.run(
        run_checkpoint_rotation(
            paths,
            snapshot_dir=_snapshot_dir(root),
            config=s04_checkpoint_config(record_threshold=2),
            fs_ops=s02_exit_ops(seam, capture=capture),
        )
    )
    image = capture.restart_image()
    candidate_temps = [
        relative
        for relative in image.files
        if "nodes_export_cache/." in relative and relative.endswith(".tmp")
    ]
    manifest_temps = [
        relative
        for relative in image.files
        if ".manifest.json." in relative and relative.endswith(".tmp")
    ]
    expected_candidate_temp, expected_manifest_temp = _KILL_TEMP_EXPECTATIONS[seam]
    assert bool(candidate_temps) is expected_candidate_temp, (seam, candidate_temps)
    assert bool(manifest_temps) is expected_manifest_temp, (seam, manifest_temps)
    paths, successor_root = _restart_after_seam(
        tmp_path, capture, root, image=image
    )
    manifest = read_manifest(paths, required=True)
    # Old epoch wholly authoritative — never a half epoch, never torn.
    assert manifest.epoch_number == 0
    assert manifest.epoch_id == "b" * 32
    assert manifest.base_incorporated_through_seq == 0
    assert manifest.journal_head_seq == 3
    assert _journal_sequences(paths) == [1, 2, 3]
    _assert_snapshot_envelopes_parse(successor_root)
    # A fresh-memory rotation publishes E+1 cleanly over the saved state.
    result = _rotate(paths, successor_root)
    assert result.status == "published", result.warnings
    manifest = read_manifest(paths, required=True)
    assert manifest.epoch_number == 1
    assert manifest.base_incorporated_through_seq == 3
    reset_fixture_memory(successor_root, accounts=(ACCOUNT_A,))


@pytest.mark.parametrize("seam", ["basis_capture", "private_replay"])
def test_kill_during_basis_capture_or_replay_matches_pre_write_outcome(tmp_path, seam, monkeypatch):
    """§11.6 basis-capture / private-replay points (and the state-equivalent
    'after checkpoint claim' point): nothing durable has mutated lineage at
    any of them, so each is outcome-equivalent to ``before_candidate_write``
    — this test IS that equivalence proof (work-order item 4: 'show which
    existing atomic seam makes them equivalent and test that equivalence').
    The seam is a test-only wrapper over the module functions the checkpoint
    body actually calls; it captures then raises."""
    import workflowy_mcp.coherence.checkpoint as checkpoint_module

    root = tmp_path / "death"
    root.mkdir()
    paths = _kill_fixture(root)
    capture = SeamCapture(root)

    real_read_records = checkpoint_module.read_journal_records
    real_materialize = checkpoint_module.materialize_payload_with_records

    def trip(*args, **kwargs):
        if os.environ.get("CUT2B_NEUTRALIZE_FAULT_SEAM"):
            # Probe: the selected seam never fires — delegate to the real
            # function so the failure is the missing pre-raise image.
            real = (
                real_read_records if seam == "basis_capture" else real_materialize
            )
            return real(*args, **kwargs)
        capture.capture(seam)
        raise InjectedFault(seam)

    with monkeypatch.context() as seam_patch:
        if seam == "basis_capture":
            seam_patch.setattr(checkpoint_module, "read_journal_records", trip)
        else:
            seam_patch.setattr(
                checkpoint_module, "materialize_payload_with_records", trip
            )
        asyncio.run(
            run_checkpoint_rotation(
                paths,
                snapshot_dir=_snapshot_dir(root),
                config=s04_checkpoint_config(record_threshold=2),
            )
        )
    paths, successor_root = _restart_after_seam(tmp_path, capture, root)
    manifest = read_manifest(paths, required=True)
    # The same outcome class as before_candidate_write: OLD epoch wholly
    # authoritative; the failed claim lived only in the dead invocation's
    # memory, so the successor reads clean lineage.
    assert manifest.epoch_number == 0
    assert manifest.epoch_id == "b" * 32
    assert manifest.base_incorporated_through_seq == 0
    assert manifest.journal_head_seq == 3
    assert _journal_sequences(paths) == [1, 2, 3]
    result = _rotate(paths, successor_root)
    assert result.status == "published", result.warnings
    manifest = read_manifest(paths, required=True)
    assert manifest.epoch_number == 1
    assert manifest.base_incorporated_through_seq == 3
    reset_fixture_memory(successor_root, accounts=(ACCOUNT_A,))


def test_kill_mid_retirement_partial_residue_retired_by_next_maintenance(tmp_path):
    """§11.6 mid-retirement / §12.2 during_journal_retirement: the FIRST
    incorporated record retires, then the seam fires before the remainder —
    a genuine PARTIAL retirement in the captured image.  The next maintenance
    pass removes the residue and NEVER deletes the active tail (seq >= floor)."""
    root = tmp_path / "death"
    root.mkdir()
    paths = _kill_fixture(root)
    capture = SeamCapture(root)
    asyncio.run(
        run_checkpoint_rotation(
            paths,
            snapshot_dir=_snapshot_dir(root),
            config=s04_checkpoint_config(record_threshold=2),
            fs_ops=s02_exit_ops("record_unlink", times=2, capture=capture),
        )
    )
    paths, successor_root = _restart_after_seam(tmp_path, capture, root)
    manifest = read_manifest(paths, required=True)
    # New epoch authoritative; retirement died after exactly ONE unlink:
    # record 1 retired, records 2 and 3 survive as partial residue.
    assert manifest.epoch_number == 1
    assert manifest.base_incorporated_through_seq == 3
    assert manifest.journal_floor_seq == 4
    assert _journal_sequences(paths) == [2, 3]
    # A new active-tail record (seq 4, AT the floor) must never be deleted.
    # RAW append (not the sealer): the sealer's own §10.7 'open' maintenance
    # would quietly retire the residue at seal time — the point HERE is to
    # prove the NEXT maintenance pass removes it.
    append_journal_transaction(
        paths,
        operation_kind="test",
        effects=[SemanticEffect.tombstone(["tail"])],
        producer=s04_producer(),
        transaction_id="tx-tail",
    )
    assert _journal_sequences(paths) == [2, 3, 4]
    # The next maintenance pass retires ONLY the below-floor residue.
    result = _rotate(paths, successor_root, threshold=500)
    assert result.status == "not_due"
    assert _journal_sequences(paths) == [4]
    manifest = read_manifest(paths, required=True)
    assert manifest.journal_head_seq == 4
    reset_fixture_memory(successor_root, accounts=(ACCOUNT_A,))


def test_kill_during_journal_retirement_leaves_retryable_residue(tmp_path):
    """§12.2 before_journal_retirement: the seam fires at the first unlink
    ATTEMPT (retirement start) — durable state identical to the
    pre-retirement window: terminal durable, zero deletions."""
    root = tmp_path / "death"
    root.mkdir()
    paths = _kill_fixture(root)
    capture = SeamCapture(root)
    asyncio.run(
        run_checkpoint_rotation(
            paths,
            snapshot_dir=_snapshot_dir(root),
            config=s04_checkpoint_config(record_threshold=2),
            fs_ops=s02_exit_ops("record_unlink", capture=capture),
        )
    )
    paths, successor_root = _restart_after_seam(tmp_path, capture, root)
    manifest = read_manifest(paths, required=True)
    # New epoch authoritative; retirement had not removed anything yet.
    assert manifest.epoch_number == 1
    assert _journal_sequences(paths) == [1, 2, 3]
    # The next maintenance pass retires the residue (even a not-due rotation
    # retries below-floor cleanup at start).
    result = _rotate(paths, successor_root, threshold=500)
    assert result.status == "not_due"
    assert _journal_sequences(paths) == []
    reset_fixture_memory(successor_root, accounts=(ACCOUNT_A,))


# ---------------------------------------------------------------------------
# §12.1 barrier / seal fault points
# ---------------------------------------------------------------------------


def test_kill_during_barrier_manifest_write_raises_nothing(tmp_path):
    root = tmp_path / "death"
    root.mkdir()
    paths = make_coherence_root(root, ACCOUNT_A)
    capture = SeamCapture(root)
    with pytest.raises(InjectedFault):
        s04_raise_barrier(
            paths,
            transaction_id="tx-kill",
            fs_ops=s02_exit_ops("manifest_replace", capture=capture),
        )
    paths, successor_root = _restart_after_seam(tmp_path, capture, root)
    manifest = read_manifest(paths, required=True)
    # The barrier was never durably written; the manifest is intact.
    assert manifest.dirty_status.active is False
    assert manifest.dirty_status.generation == 0
    reset_fixture_memory(successor_root, accounts=(ACCOUNT_A,))


def test_kill_at_record_write_commits_nothing_and_raises_no_barrier(tmp_path):
    root = tmp_path / "death"
    root.mkdir()
    paths = make_coherence_root(root, ACCOUNT_A)
    capture = SeamCapture(root)
    with pytest.raises(InjectedFault):
        s04_append(
            paths,
            transaction_id="tx-kill",
            fs_ops=s02_exit_ops("record_replace", capture=capture),
        )
    paths, successor_root = _restart_after_seam(tmp_path, capture, root)
    manifest = read_manifest(paths, required=True)
    # The PRE-RAISE image: nothing committed and NO barrier substituted from
    # the sealer's post-unwind barrier write.
    assert manifest.journal_head_seq == 0
    assert manifest.dirty_status.active is False
    assert _journal_sequences(paths) == []
    reset_fixture_memory(successor_root, accounts=(ACCOUNT_A,))


# ---------------------------------------------------------------------------
# Concurrency: barrier vs append; one-flight checkpoint; atomic readers
# ---------------------------------------------------------------------------


def test_barrier_and_append_serialize_without_lost_state(tmp_path, monkeypatch):
    """Five synchronous appends and three barriers interleave only between tasks."""
    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    errors: list[BaseException] = []
    from coherence_harness import cut2b_neutralize_barrier_advance, s04_append
    cut2b_neutralize_barrier_advance(monkeypatch)

    async def _appends() -> None:
        try:
            for _ in range(5):
                s04_append(paths)
                await asyncio.sleep(0)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    async def _barriers() -> None:
        try:
            for index in range(3):
                s04_raise_barrier(paths, transaction_id=f"tx-concurrent-{index}")
                await asyncio.sleep(0)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    async def scenario():
        await asyncio.gather(_appends(), _barriers())
    asyncio.run(scenario())
    assert errors == []
    manifest = read_manifest(paths, required=True)
    assert manifest.journal_head_seq == 5
    assert manifest.dirty_status.active is True
    assert manifest.dirty_status.generation == 3
    # The journal tail remains contiguous (no torn interleaving).
    assert _journal_sequences(paths) == [1, 2, 3, 4, 5]


def test_concurrent_rotations_publish_exactly_once(tmp_path, monkeypatch):
    """Two loop tasks: one active record admits exactly one checkpoint owner."""
    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    _based_manifest(paths, tmp_path)
    s04_append_n(paths, 3)
    outcomes: list = []
    errors: list[BaseException] = []

    from workflowy_mcp.coherence import checkpoint as cp
    real_phase = cp._write_claim_status
    async def scenario():
        parked, release = asyncio.Event(), asyncio.Event()
        first = True
        async def phase(paths_arg, record, status, **changes):
            nonlocal first
            if first and changes.get("candidate_snapshot_path"):
                first = False
                parked.set()
                await release.wait()
            return await real_phase(paths_arg, record, status, **changes)
        monkeypatch.setattr(cp, "_write_claim_status", phase)
        async def rotate():
            return await cp.run_checkpoint_rotation(
                paths, snapshot_dir=_snapshot_dir(tmp_path), config=s04_checkpoint_config(record_threshold=2),
            )
        owner = asyncio.create_task(rotate())
        try:
            await parked.wait()
            if os.environ.get("CUT2B_NEUTRALIZE_PUBLICATION_RECORD"):
                monkeypatch.setattr(cp, "_read_publication_record", lambda paths: None)
            contender = await rotate()
            assert contender.status == "deferred_publisher_busy", "second task acquired while first record was active"
            outcomes.append(contender)
        finally:
            release.set()
            outcomes.append(await owner)
    asyncio.run(scenario())
    assert errors == []
    statuses = sorted(result.status for result in outcomes)
    # Exactly ONE publication (the other racer either defers on the live
    # claim or finds the tail already compacted — both are correct one-flight
    # outcomes; the epoch advances exactly once either way).
    assert statuses.count("published") == 1
    assert statuses[0] in ("deferred_publisher_busy", "not_due")
    assert statuses[1] == "published"
    manifest = read_manifest(paths, required=True)
    assert manifest.epoch_number == 1  # epoch advanced EXACTLY once


def test_lock_free_readers_observe_only_complete_manifests(tmp_path):
    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    _based_manifest(paths, tmp_path)
    s04_append_n(paths, 3)
    stop = threading.Event()
    result_holder: dict = {}

    def _reader() -> None:
        result_holder["result"] = run_reader_loop(paths.manifest_path, stop, poll=0.0005)

    reader = threading.Thread(target=_reader)
    reader.start()
    rotate_result = _rotate(paths, tmp_path)
    stop.set()
    reader.join()
    assert rotate_result.status == "published", rotate_result.warnings
    reader_result = result_holder["result"]
    assert reader_result.torn == 0
    assert reader_result.unexpected == []
    assert reader_result.reads > 0
