"""S02 §16.1/§14.3 — fault-injection tests over the named durable seams.

CUT-2B: the former process crash matrix is now a TEST-ONLY named-seam
exception matrix.  Each seam captures the isolated root's relative-path→bytes
durable image BEFORE raising; the invocation unwinds and the successor is
reconstructed from that image with fresh in-memory authority.
"""

from __future__ import annotations

import asyncio
import os
from functools import wraps
from pathlib import Path

import pytest
from coherence_harness import (
    ACCOUNT_A,
    InjectedFault,
    RecordedSleep,
    SeamCapture,
    make_coherence_root,
    reset_fixture_memory,
    restore_successor,
    s02_exit_ops,
    s02_fetch,
    s02_make_export,
    s02_node,
    s02_set_barrier,
    s02_set_external_dirt,
)

from workflowy_mcp.coherence import (
    inspect_journal,
    read_external_dirt,
    read_manifest,
    run_full_refresh,
)


def async_test(function):
    @wraps(function)
    def wrapper(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))

    return wrapper


def _snapshot_dir(tmp_path: Path) -> Path:
    target = tmp_path / "snapshots"
    target.mkdir(parents=True, exist_ok=True)
    return target


# ---------------------------------------------------------------------------
# Two daemon tasks, same account, covered generations → ONE fetch
# ---------------------------------------------------------------------------


@async_test
async def test_two_processes_concurrent_refresh_one_fetch(tmp_path, monkeypatch):
    """Historical name: owner and covered joiner share one memory record."""
    from workflowy_mcp.coherence import refresh_authority as ra
    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    snapshot_dir = _snapshot_dir(tmp_path)
    holding, joined, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    fetches = []
    real_join = ra._join_refresh
    async def observe_join(*args, **kwargs):
        joined.set()
        return await real_join(*args, **kwargs)
    monkeypatch.setattr(ra, "_join_refresh", observe_join)
    async def owner_fetch():
        fetches.append("x")
        holding.set()
        await release.wait()
        return {"nodes": [{"id": "n1", "name": "one"}]}
    async def joiner_fetch():
        fetches.append("x")
        raise AssertionError("joiner must never fetch")
    owner = asyncio.create_task(run_full_refresh(
        paths=paths, fetch=owner_fetch, settle_seconds=0, snapshot_dir=snapshot_dir, poll_seconds=0.05,
    ))
    joiner = None
    try:
        await asyncio.wait_for(holding.wait(), 10)
        joiner = asyncio.create_task(run_full_refresh(
            paths=paths, fetch=joiner_fetch, settle_seconds=0, snapshot_dir=snapshot_dir, poll_seconds=0.05,
        ))
        await asyncio.wait_for(joined.wait(), 10)
        release.set()
        o_result, j_result = await asyncio.gather(owner, joiner)
        assert o_result.status == "published"
        assert j_result.status == "joined"
        assert fetches == ["x"]
        assert read_manifest(paths, required=True).epoch_number == 1
    finally:
        release.set()
        await asyncio.gather(*(task for task in (owner, joiner) if task is not None), return_exceptions=True)
        ra._PUBLICATION_RECORDS.pop(ra._publication_key(paths), None)


# ---------------------------------------------------------------------------
# Named-seam fault matrix (§14.3) — pre-raise image + fresh-memory successor
# ---------------------------------------------------------------------------

#: ``fault_point -> (candidate temp durable, manifest temp durable)`` at the
#: PRE-RAISE instant (the atomic writer unlinks its own temp on unwind).
_S02_TEMP_EXPECTATIONS = {
    "candidate_temp_write": (False, False),
    "manifest_temp_write": (False, False),
    "manifest_replace": (False, True),
    "dirt_replace": (False, False),
}


@pytest.mark.parametrize(
    ("fault_point", "times", "expect_published"),
    [
        ("candidate_temp_write", 1, False),  # before candidate durability
        ("manifest_temp_write", 1, False),  # candidate durable, manifest not published
        ("manifest_replace", 1, False),  # manifest publication interrupted
    ],
    ids=["before_candidate", "before_manifest_write", "at_manifest_replace"],
)
def test_dead_owner_crash_recovers_and_succeeds(tmp_path, fault_point, times, expect_published):
    """Historical name: a named seam replaces the dead process owner.

    The failed invocation's memory dies with it (fresh successor); the
    successor fixture is built from the PRE-RAISE image, never from the
    post-unwind files.
    """
    root = tmp_path / "death"
    root.mkdir()
    paths = make_coherence_root(root, ACCOUNT_A)
    capture = SeamCapture(root)

    async def crashed() -> None:
        await run_full_refresh(
            paths=paths,
            fetch=s02_fetch(s02_make_export([s02_node("crashed")])),
            settle_seconds=0,
            snapshot_dir=_snapshot_dir(root),
            poll_seconds=0.05,
            fs_ops=s02_exit_ops(fault_point, times=times, capture=capture),
        )

    with pytest.raises(InjectedFault):
        asyncio.run(crashed())
    assert capture.images, f"named seam {fault_point} never tripped"

    image = capture.restart_image()
    candidate_temps = [
        relative
        for relative in image.files
        if "snapshots/." in relative and relative.endswith(".tmp")
    ]
    manifest_temps = [
        relative
        for relative in image.files
        if ".manifest.json." in relative and relative.endswith(".tmp")
    ]
    expected_candidate_temp, expected_manifest_temp = _S02_TEMP_EXPECTATIONS[fault_point]
    assert bool(candidate_temps) is expected_candidate_temp, (fault_point, candidate_temps)
    assert bool(manifest_temps) is expected_manifest_temp, (fault_point, manifest_temps)

    successor_root = root
    reset_fixture_memory(root, accounts=(ACCOUNT_A,))
    paths = restore_successor(image, successor_root, ACCOUNT_A)
    reset_fixture_memory(successor_root, accounts=(ACCOUNT_A,))
    try:
        # The old invocation's ownership memory died with it; a fresh refresh
        # over the captured durable state succeeds.
        outcome = asyncio.run(
            run_full_refresh(
                paths=paths,
                fetch=s02_fetch(s02_make_export([s02_node("fresh")])),
                settle_seconds=0,
                snapshot_dir=_snapshot_dir(successor_root),
                sleep=RecordedSleep(),
                poll_seconds=0.02,
            )
        )
        assert outcome.status == "published"
        manifest = read_manifest(paths, required=True)
        if expect_published:
            # The interrupted owner DID publish its epoch before the seam;
            # the successor manifest survived, and the new refresh advanced.
            assert manifest.epoch_number == 2
        else:
            # The interrupted owner never published; this refresh is epoch 1.
            assert manifest.epoch_number == 1
        report = inspect_journal(paths, manifest)
        assert report["contiguous_active_range"]
    finally:
        reset_fixture_memory(root, accounts=(ACCOUNT_A,))
        reset_fixture_memory(successor_root, accounts=(ACCOUNT_A,))


def test_crash_after_manifest_publish_before_dirt_clear_keeps_dirt(tmp_path):
    """The seam lands after manifest publish but before the external-dirt
    clear: the captured death image keeps the new epoch valid and the
    post-cut-eligible dirt active."""
    root = tmp_path / "death"
    root.mkdir()
    paths = make_coherence_root(root, ACCOUNT_A)
    s02_set_external_dirt(paths, 1, active=True)
    capture = SeamCapture(root)

    async def crashed() -> object:
        return await run_full_refresh(
            paths=paths,
            fetch=s02_fetch(s02_make_export([s02_node("crashed")])),
            settle_seconds=0,
            snapshot_dir=_snapshot_dir(root),
            poll_seconds=0.05,
            fs_ops=s02_exit_ops("dirt_replace", capture=capture),
        )

    # The dirt-clear step is advisory inside publish_epoch: the seam's
    # exception is converted to the existing external_dirt_clear_failed
    # advisory AFTER the image was captured — the epoch itself published.
    outcome = asyncio.run(crashed())
    assert capture.images, "named seam dirt_replace never tripped"
    assert outcome.external_dirt_clear_failed is True
    image = capture.restart_image()
    dirt_temps = [
        relative
        for relative in image.files
        if ".external_dirt.json." in relative and relative.endswith(".tmp")
    ]
    assert dirt_temps, "the dirt temp was not durable at the pre-raise instant"

    successor_root = root
    reset_fixture_memory(root, accounts=(ACCOUNT_A,))
    paths = restore_successor(image, successor_root, ACCOUNT_A)
    reset_fixture_memory(successor_root, accounts=(ACCOUNT_A,))
    try:
        manifest = read_manifest(paths, required=True)
        # The epoch published before the interrupted dirt clear.
        assert manifest.epoch_number == 1
        # The dirt was NOT cleared (the clearing write was the seam point).
        dirt = read_external_dirt(paths)
        assert dirt is not None and dirt.active is True
        # A follow-up refresh (cut covers gen 1) clears it.
        outcome = asyncio.run(
            run_full_refresh(
                paths=paths,
                fetch=s02_fetch(s02_make_export([s02_node("fresh")])),
                settle_seconds=0,
                snapshot_dir=_snapshot_dir(successor_root),
                sleep=RecordedSleep(),
                poll_seconds=0.02,
            )
        )
        assert outcome.external_dirt_cleared is True
        assert read_external_dirt(paths).active is False
    finally:
        reset_fixture_memory(root, accounts=(ACCOUNT_A,))
        reset_fixture_memory(successor_root, accounts=(ACCOUNT_A,))


# ---------------------------------------------------------------------------
# The mandatory epoch-race scenario (Waypoint A): in-flight work spanning a
# refresh epoch cut must replay cleanly or trip a NEWER surviving barrier.
# ---------------------------------------------------------------------------


@async_test
async def test_inflight_append_and_barrier_spanning_epoch_cut(tmp_path):
    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    from workflowy_mcp.coherence import (
        JournalProducer,
        SemanticEffect,
        append_journal_transaction,
    )

    async def fetch():
        # Post-cut, pre-incorporation: a journal append and a NEWER barrier.
        append_journal_transaction(
            paths,
            operation_kind="create",
            effects=[SemanticEffect.node_create(s02_node("inflight"))],
            producer=JournalProducer(
                pid=os.getpid(), pid_create_time=1.0,
                owner_kind="process", ownership_kind="none",
            ),
        )
        s02_set_barrier(paths, 1, active=True)
        return s02_make_export([s02_node("a")])

    outcome = await run_full_refresh(
        paths=paths,
        fetch=fetch,
        settle_seconds=0,
        snapshot_dir=_snapshot_dir(tmp_path),
        sleep=RecordedSleep(),
        poll_seconds=0.02,
    )
    manifest = read_manifest(paths, required=True)
    # The in-flight record was folded into the new base (never lost).
    ids = {n["id"] for n in outcome.nodes_export["nodes"]}
    assert "inflight" in ids
    # The post-cut barrier SURVIVES the publication (never silently cleared).
    assert manifest.dirty_status.active is True
    assert manifest.dirty_status.generation == 1
    # A recovery refresh clears the covered barrier; lineage stays coherent.
    # (The mock export below is vault-faithful: a journaled create IS
    # committed server-side, so a real /nodes-export contains the node.)
    outcome2 = await run_full_refresh(
        paths=paths,
        fetch=s02_fetch(s02_make_export([s02_node("a"), s02_node("inflight")])),
        refresh_kind="recovery",
        settle_seconds=0,
        snapshot_dir=_snapshot_dir(tmp_path),
        sleep=RecordedSleep(),
        poll_seconds=0.02,
    )
    assert outcome2.hard_barrier_cleared is True
    manifest2 = read_manifest(paths, required=True)
    assert manifest2.dirty_status.active is False
    # The folded record persists across the second cut as well.
    ids2 = {n["id"] for n in outcome2.nodes_export["nodes"]}
    assert "inflight" in ids2
