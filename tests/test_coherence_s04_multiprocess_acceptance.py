"""S04 Remediation Chain 2 (work-order item 3) — the Family-5 acceptance
matrix, deterministic and same-loop (CUT-2B Round 4).

Every actor below is a real asyncio task on the ONE daemon loop sharing one
coherence root; synchronization is explicit ``asyncio.Event`` gates and
injected test seams (a deliberately test-held async coherence lock, a pause
in the publication path), never probabilistic sleeps, process scheduling or
sentinel files.  Historical test names are retained; no process is launched.

1. barrier raiser vs journal appender — TWO DISTINCT COVERAGES:
   (a) deterministic interleaving (retained, honestly relabeled in
       Round 3): a forced mid-run crossing makes both tasks provably
       unfinished, so the raiser's first barrier lands between the
       appender's 2nd and 3rd append.  The two actors' coherence-lock
       acquisitions never overlap — this is NOT mutex-contention evidence;
   (b) GENUINE in-process coherence-lock contention: the test deliberately
       holds the async lock and parks; the contender must enter its existing
       poll branch, show ticker progress and zero transaction entry before
       release (``CUT2B_NEUTRALIZE_COHERENCE_ACQUIRE`` lets it enter early
       and fails the pre-release assertion).  A synchronous fsync is never
       parked waiting for another task.
2. two checkpoint requesters: task gates; the winning owner is paused AFTER
   claim acquisition; the loser provably attempts while THAT fixed claim is
   active — its dedupe disposition is reachable ONLY under an active
   covering checkpoint claim;
3. checkpoint-first vs full refresh: the checkpoint is released only after
   the refresh task acknowledges it entered the contended acquisition path
   (never merely a pre-call launched marker), with the fixed claim verified
   still active and winner-owned at the release instant;
4. full-refresh-first vs checkpoint: the checkpoint defer task runs only
   after the refresh holds the claim, and its defer disposition is reachable
   only under an active non-checkpoint claim;
5. U and C as separate loop tasks, each publishing only its own transaction:
   forced U→C and C→U orders; replay / checkpoint / reload equality in the
   parent (a third step) keeps the same three oracles.
   ``CUT2B_NEUTRALIZE_COMMUTATION`` makes the UPDATE task wrongly replace
   parent/membership and must fail that equality.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from test_coherence_s02_refresh_claim import async_test

from coherence_harness import (
    ACCOUNT_A,
    assert_semantic_cache_equal,
    make_coherence_root,
    reset_fixture_memory,
    s02_node,
    s04_append,
    s04_append_n,
    s04_checkpoint_config,
    s04_install_base_manifest,
    s04_write_base_snapshot,
)

from workflowy_mcp.coherence import (
    SemanticEffect,
    materialize_current_payload,
    parse_journal_record_filename,
    read_manifest,
    run_checkpoint_rotation,
    run_full_refresh,
)
from workflowy_mcp.coherence import checkpoint as cp
from workflowy_mcp.coherence import refresh_authority as ra


def _journal_sequences(paths) -> list[int]:
    if not paths.journal_dir.exists():
        return []
    return sorted(
        seq
        for entry in paths.journal_dir.iterdir()
        if (seq := parse_journal_record_filename(entry.name)) is not None
    )


def _based_root(root: Path, *, nodes: list[dict] | None = None, records: int = 3):
    paths = make_coherence_root(root, ACCOUNT_A)
    snapshot_dir = root / "nodes_export_cache"
    base = s04_write_base_snapshot(
        paths,
        snapshot_dir,
        nodes or [s02_node("base")],
        epoch_number=0,
        epoch_id="b" * 32,
    )
    s04_install_base_manifest(paths, base_snapshot_path=base, epoch_id="b" * 32)
    if records:
        s04_append_n(paths, records)
    return paths, snapshot_dir


# ---------------------------------------------------------------------------
# 1. Same-loop append/barrier ordering, plus a deliberate short-lock contender.
#    The synchronous transaction ordering is NOT lock-contention evidence;
#    the separate test-held async lock proves the poll/no-entry observation.
# ---------------------------------------------------------------------------

def test_barrier_raiser_and_journal_appender_deterministic_interleaving(tmp_path, monkeypatch):
    """Barrier zero lands between append two and three; no contention claim."""
    from coherence_harness import cut2b_neutralize_barrier_advance, s04_raise_barrier
    cut2b_neutralize_barrier_advance(monkeypatch)
    paths = make_coherence_root(tmp_path / "share", ACCOUNT_A)
    async def scenario():
        mid, raiser_ready, go_raise, go_app = (asyncio.Event() for _ in range(4))
        async def append():
            s04_append_n(paths, 2)
            mid.set()
            await go_app.wait()
            s04_append_n(paths, 3)
        async def raise_barriers():
            await mid.wait()
            raiser_ready.set()
            await go_raise.wait()
            s04_raise_barrier(paths, transaction_id="tx-actor-0")
            go_app.set()
            await asyncio.sleep(0)
            s04_raise_barrier(paths, transaction_id="tx-actor-1")
            s04_raise_barrier(paths, transaction_id="tx-actor-2")
        appender, raiser = asyncio.create_task(append()), asyncio.create_task(raise_barriers())
        await raiser_ready.wait()
        assert not appender.done() and not raiser.done()
        assert read_manifest(paths).journal_head_seq == 2
        go_raise.set()
        await asyncio.gather(appender, raiser)
    asyncio.run(scenario())
    manifest = read_manifest(paths, required=True)
    assert manifest.journal_head_seq == 5
    assert _journal_sequences(paths) == [1, 2, 3, 4, 5]
    assert manifest.dirty_status.active is True
    assert manifest.dirty_status.generation == 3


# ---------------------------------------------------------------------------
# 1b. Deliberate test-held async lock and real task polling, then the
#     unchanged append/barrier durable-state outcome.
# ---------------------------------------------------------------------------

def test_barrier_raiser_and_journal_appender_genuine_mutex_contention(tmp_path, monkeypatch):
    """A real contender polls a held async lock; synchronous writes never park fsync."""
    from workflowy_mcp import carto_job_governance as gov
    from workflowy_mcp.coherence import barrier, paths as locks
    paths = make_coherence_root(tmp_path / "share", ACCOUNT_A)
    s04_append_n(paths, 2)
    entered, checks, ticks = [], [], []
    real_check, real_body = gov._check_cancel_async, barrier._raise_global_dirty_barrier_locked
    contender = None
    async def check(cancel, location):
        if asyncio.current_task() is contender:
            checks.append(location)
        return await real_check(cancel, location)
    def body(*args, **kwargs):
        entered.append(True)
        return real_body(*args, **kwargs)
    monkeypatch.setattr(gov, "_check_cancel_async", check)
    monkeypatch.setattr(barrier, "_raise_global_dirty_barrier_locked", body)
    if os.environ.get("CUT2B_NEUTRALIZE_COHERENCE_ACQUIRE"):
        monkeypatch.setattr(locks, "_coherence_lock_for", lambda paths: asyncio.Lock())
    async def scenario():
        nonlocal contender
        async def ticker():
            while True:
                ticks.append(True)
                await asyncio.sleep(0)
        clock = asyncio.create_task(ticker())
        try:
            async with locks.coherence_lock_async(paths):
                contender = asyncio.create_task(barrier.raise_global_dirty_barrier_async(
                    paths, reason_code="journal_publication_failed", transaction_id="tx-contender",
                ))
                await asyncio.sleep(0)
                assert entered == [], "barrier entered while test-held coherence lock was parked"
                assert len(checks) >= 2, "contender never entered its busy-poll branch"
                assert not contender.done() and ticks
            result = await contender
            s04_append_n(paths, 3)
            return {"generation": result.state.generation, "disposition": result.disposition}
        finally:
            if contender is not None:
                await asyncio.gather(contender, return_exceptions=True)
            clock.cancel()
            try:
                await clock
            except asyncio.CancelledError:
                pass
    r_raise = asyncio.run(scenario())
    assert r_raise == {"generation": 1, "disposition": "raised"}, r_raise
    manifest = read_manifest(paths, required=True)
    assert manifest.journal_head_seq == 5
    assert _journal_sequences(paths) == [1, 2, 3, 4, 5]
    assert manifest.dirty_status.active is True
    assert manifest.dirty_status.generation == 1
    assert manifest.dirty_status.transaction_id == "tx-contender"


# ---------------------------------------------------------------------------
# 2. Two checkpoint requesters as separate loop tasks (historical name)
# ---------------------------------------------------------------------------


@async_test
async def test_two_checkpoint_requesters_as_separate_subprocesses(tmp_path, monkeypatch):
    """Historical name: ready/go gates two tasks; loser sees the parked record."""
    root = tmp_path / "share"
    paths, snapshot_dir = _based_root(root, records=3)
    ready = {name: asyncio.Event() for name in ("A", "B")}
    go, holding, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    winner = []
    real_publish = cp.publish_epoch
    async def paused_publish(**kwargs):
        winner.append(asyncio.current_task().get_name())
        holding.set()
        await release.wait()
        return await real_publish(**kwargs)
    monkeypatch.setattr(cp, "publish_epoch", paused_publish)
    async def racer(name):
        ready[name].set()
        await go.wait()
        return await run_checkpoint_rotation(
            paths, snapshot_dir=snapshot_dir, config=s04_checkpoint_config(record_threshold=2),
        )
    tasks = {name: asyncio.create_task(racer(name), name=name) for name in ("A", "B")}
    try:
        for event in ready.values():
            await asyncio.wait_for(event.wait(), 60)
        go.set()
        await asyncio.wait_for(holding.wait(), 90)
        assert len(winner) == 1
        loser = "B" if winner[0] == "A" else "A"
        loser_result = await asyncio.wait_for(tasks[loser], 60)
        assert loser_result.status == "deferred_publisher_busy"
        assert any("deduplicated" in warning for warning in loser_result.warnings)
        claim = ra._read_publication_record(paths)
        assert claim is not None and not claim.is_terminal
        assert claim.refresh_kind == "checkpoint"
        release.set()
        r_winner = await tasks[winner[0]]
        assert r_winner.status == "published", r_winner.warnings
        manifest = read_manifest(paths, required=True)
        assert manifest.epoch_number == 1
        assert manifest.base_incorporated_through_seq == 3
    finally:
        go.set()
        release.set()
        await asyncio.gather(*tasks.values(), return_exceptions=True)
        ra._PUBLICATION_RECORDS.pop(ra._publication_key(paths), None)


# ---------------------------------------------------------------------------
# 3. Checkpoint publisher vs full-refresh publisher — both claim-first orders
# ---------------------------------------------------------------------------


@async_test
async def test_checkpoint_first_full_refresh_waits_and_publishes_after(tmp_path, monkeypatch):
    """Two tasks; refresh acknowledges waiting before the checkpoint releases."""
    root = tmp_path / "share"
    paths, snapshot_dir = _based_root(root, records=3)
    holding, contending, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    fetches = []
    real_publish = cp.publish_epoch
    async def paused_publish(**kwargs):
        holding.set()
        await release.wait()
        return await real_publish(**kwargs)
    monkeypatch.setattr(cp, "publish_epoch", paused_publish)
    async def ack_sleep(seconds):
        if seconds > 0:
            contending.set()
        await asyncio.sleep(seconds)
    async def fetch():
        fetches.append("fetch")
        # Let the existing zero-fetch oracle reject an illegally early owner
        # directly instead of waiting for the contention acknowledgment.
        contending.set()
        return {"nodes": [s02_node("fr-node")]}
    ck = asyncio.create_task(run_checkpoint_rotation(
        paths, snapshot_dir=snapshot_dir, config=s04_checkpoint_config(record_threshold=2),
    ))
    fr = None
    try:
        await asyncio.wait_for(holding.wait(), 90)
        fr = asyncio.create_task(run_full_refresh(
            paths=paths, fetch=fetch, settle_seconds=0, snapshot_dir=snapshot_dir,
            sleep=ack_sleep, poll_seconds=0.05, max_wait_seconds=60,
        ))
        await asyncio.wait_for(contending.wait(), 60)
        claim = ra._read_publication_record(paths)
        assert claim is not None and not claim.is_terminal
        assert claim.refresh_kind == "checkpoint"
        assert fetches == []
        release.set()
        r_ck, r_fr = await asyncio.gather(ck, fr)
        assert r_ck.status == "published", r_ck.warnings
        assert r_ck.incorporated_through_seq == 3
        assert r_fr.status == "published" and r_fr.epoch_number == 2
        assert read_manifest(paths, required=True).epoch_number == 2
    finally:
        release.set()
        await asyncio.gather(*(task for task in (ck, fr) if task is not None), return_exceptions=True)
        ra._PUBLICATION_RECORDS.pop(ra._publication_key(paths), None)



@async_test
async def test_full_refresh_first_checkpoint_defers_then_publishes_after(tmp_path):
    """Two tasks; checkpoint returns before release, then a later one owns E+2."""
    root = tmp_path / "share"
    paths, snapshot_dir = _based_root(root, records=3)
    holding, release = asyncio.Event(), asyncio.Event()
    async def fetch():
        holding.set()
        await release.wait()
        return {"nodes": [s02_node("fr-node")]}
    fr = asyncio.create_task(run_full_refresh(
        paths=paths, fetch=fetch, settle_seconds=0, snapshot_dir=snapshot_dir,
        poll_seconds=0.05, max_wait_seconds=60,
    ))
    try:
        await asyncio.wait_for(holding.wait(), 90)
        r_ck = await asyncio.wait_for(run_checkpoint_rotation(
            paths, snapshot_dir=snapshot_dir, config=s04_checkpoint_config(record_threshold=2),
        ), 120)
        assert r_ck.status == "deferred_publisher_busy"
        assert not release.is_set()
        release.set()
        r_fr = await fr
        assert r_fr.status == "published" and r_fr.epoch_number == 1
        s04_append_n(paths, 2)
        result = await run_checkpoint_rotation(
            paths, snapshot_dir=snapshot_dir, config=s04_checkpoint_config(record_threshold=2),
        )
        assert result.status == "published", result.warnings
        assert read_manifest(paths, required=True).epoch_number == 2
    finally:
        release.set()
        await asyncio.gather(fr, return_exceptions=True)
        ra._PUBLICATION_RECORDS.pop(ra._publication_key(paths), None)


# ---------------------------------------------------------------------------
# 4. U and C as separate loop tasks — forced orders, same three oracles
# ---------------------------------------------------------------------------


def _run_actor_order(tmp_path: Path, order: str, label: str) -> dict:
    """Force one U/C publication order across TWO loop tasks (each publishes
    ONLY its own transaction), then replay / checkpoint / reload in a third
    step.  Historical name retained; no process is launched."""

    root = tmp_path / label
    root.mkdir()
    nodes = [
        s02_node("P", name="Parent", priority=100),
        s02_node("A", name="A-original", parent_id="P", priority=100),
        s02_node("B", name="B", parent_id="P", priority=200),
    ]
    paths, snapshot_dir = _based_root(root, nodes=nodes, records=0)
    # The probe makes UPDATE wrongly replace parent/membership (a stale
    # structural write), so the across-order equality oracles must fail.
    update_effects = [
        SemanticEffect.node_fields(
            "A", ["name", "note"], {"name": "A-updated", "note": "note-from-U"}
        )
    ]
    if os.environ.get("CUT2B_NEUTRALIZE_COMMUTATION"):
        update_effects.append(SemanticEffect.node_parentage("A", None))
    create_effects = [
        SemanticEffect.node_create(
            s02_node("C", name="C-created", parent_id="P", priority=150)
        ),
        SemanticEffect.child_order(
            "P",
            ["A", "C", "B"],
            priority_by_id={"A": 100, "C": 150, "B": 200},
            canonical_records={
                "C": s02_node("C", name="C-created", parent_id="P", priority=150)
            },
        ),
    ]

    async def scenario() -> None:
        go_u, go_c = asyncio.Event(), asyncio.Event()
        u_done, c_done = asyncio.Event(), asyncio.Event()

        async def u_actor() -> None:
            await go_u.wait()
            s04_append(
                paths,
                operation_kind="update",
                effects=update_effects,
                transaction_id="tx-U",
            )
            u_done.set()

        async def c_actor() -> None:
            await go_c.wait()
            s04_append(
                paths,
                operation_kind="create",
                effects=create_effects,
                transaction_id="tx-C",
            )
            c_done.set()

        u_task = asyncio.create_task(u_actor(), name="U")
        c_task = asyncio.create_task(c_actor(), name="C")
        await asyncio.sleep(0)  # both tasks parked at their gates
        first_go, first_done, second_go, second_done = (
            (go_u, u_done, go_c, c_done)
            if order == "UC"
            else (go_c, c_done, go_u, u_done)
        )
        first_go.set()
        await asyncio.wait_for(first_done.wait(), 60)
        assert not second_go.is_set(), "second actor ran before the forced order"
        second_go.set()
        await asyncio.wait_for(second_done.wait(), 60)
        await asyncio.gather(u_task, c_task)

    try:
        asyncio.run(scenario())
        manifest = read_manifest(paths, required=True)
        assert manifest.journal_head_seq == 2  # exactly the two transactions
        replayed = materialize_current_payload(paths, manifest).payload
        rotation = asyncio.run(
            run_checkpoint_rotation(
                paths,
                snapshot_dir=snapshot_dir,
                config=s04_checkpoint_config(record_threshold=1),
            )
        )
        assert rotation.status == "published", rotation.warnings
        manifest = read_manifest(paths, required=True)
        reloaded = materialize_current_payload(paths, manifest).payload
        return {"replayed": replayed, "reloaded": reloaded}
    finally:
        reset_fixture_memory(root)


def test_update_and_create_actors_commute_in_forced_orders(tmp_path):
    """U (exact-node UPDATE, owns A's name/note) and C (sibling CREATE +
    child-order, owns membership/order/priority ranks) as SEPARATE loop
    tasks; forced U→C and C→U.  Both orders converge to identical cache
    state across replay, checkpoint, and epoch reload (Axiom 7).
    ``CUT2B_NEUTRALIZE_COMMUTATION`` makes UPDATE replace parent/membership
    and must fail that equality."""

    uc = _run_actor_order(tmp_path, "UC", "order_uc")
    cu = _run_actor_order(tmp_path, "CU", "order_cu")

    # The same three oracles: replayed ≡ checkpoint-reloaded ≡ (across orders).
    assert_semantic_cache_equal(uc["replayed"], cu["replayed"])
    assert_semantic_cache_equal(uc["reloaded"], cu["reloaded"])
    assert_semantic_cache_equal(uc["replayed"], uc["reloaded"])
    assert_semantic_cache_equal(cu["replayed"], cu["reloaded"])

    by_id = {n["id"]: n for n in cu["reloaded"]["nodes"]}
    # A's content comes from U, never overwritten by C's child-order records.
    assert by_id["A"]["name"] == "A-updated"
    assert by_id["A"]["note"] == "note-from-U"
    # Membership/order/priority come from C's structural effect.
    assert set(by_id) == {"P", "A", "B", "C"}
    assert by_id["C"]["name"] == "C-created"
    assert by_id["C"]["parent_id"] == "P"
    children = [n for n in cu["reloaded"]["nodes"] if n.get("parent_id") == "P"]
    ordered = sorted(children, key=lambda n: n.get("priority", 0))
    assert [n["id"] for n in ordered] == ["A", "C", "B"]
