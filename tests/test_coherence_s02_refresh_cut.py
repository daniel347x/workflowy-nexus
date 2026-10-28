"""S02 §16.2 — refresh cut and settle-delay ordering tests."""

from __future__ import annotations

import asyncio
from functools import wraps
from pathlib import Path

import pytest
from test_cut_2a_publication import _publication_records_cleanup
from coherence_harness import (
    ACCOUNT_A,
    RecordedSleep,
    make_coherence_root,
    s02_fetch,
    s02_make_export,
    s02_node,
    s02_set_barrier,
    s02_set_external_dirt,
)

from workflowy_mcp.coherence import (
    read_external_dirt,
    read_manifest,
    run_full_refresh,
)
from workflowy_mcp.coherence.errors import CoherenceError
from workflowy_mcp.coherence.refresh_authority import _read_publication_record


def async_test(function):
    @wraps(function)
    def wrapper(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))

    return wrapper


def _snapshot_dir(tmp_path: Path) -> Path:
    target = tmp_path / "snapshots"
    target.mkdir(parents=True, exist_ok=True)
    return target


@async_test
async def test_cut_then_settle_then_fetch_exact_order(tmp_path):
    """Exact order: capture cut → store memory record → injected sleep → fetch."""
    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    events: list[str] = []

    class OrderSleep:
        async def __call__(self, seconds: float) -> None:
            # The record (with its cut) must already exist in memory, in the
            # settling state, BEFORE the delay begins.
            claim = _read_publication_record(paths)
            assert claim is not None and claim.status == "settling"
            assert claim.cut_token is not None
            events.append(f"settle:{seconds}")
            await asyncio.sleep(0)

    async def fetch():
        events.append("fetch")
        return s02_make_export([s02_node("a")])

    outcome = await run_full_refresh(
        paths=paths,
        fetch=fetch,
        settle_seconds=2.0,
        snapshot_dir=_snapshot_dir(tmp_path),
        sleep=OrderSleep(),
        poll_seconds=0.02,
    )
    assert outcome.status == "published"
    assert events == ["settle:2.0", "fetch"]
    assert outcome.settle_seconds == 2.0


@async_test
async def test_zero_settle_still_passes_through_sleep_hook(tmp_path):
    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    sleep = RecordedSleep()
    outcome = await run_full_refresh(
        paths=paths,
        fetch=s02_fetch(s02_make_export([s02_node("a")])),
        settle_seconds=0,
        snapshot_dir=_snapshot_dir(tmp_path),
        sleep=sleep,
        poll_seconds=0.02,
    )
    assert outcome.status == "published"
    assert sleep.durations == [0.0]


def test_default_settle_is_two_seconds():
    import inspect

    from workflowy_mcp.coherence import refresh_authority

    signature = inspect.signature(refresh_authority.run_full_refresh)
    assert signature.parameters["settle_seconds"].default == 2.0
    from workflowy_mcp.models.config import TimingConfiguration

    assert TimingConfiguration().refresh_settle_seconds == 2.0


@async_test
async def test_signal_during_settle_survives(tmp_path):
    """A barrier/dirt signal raised DURING the settle delay post-dates the
    cut and must survive publication."""
    paths = make_coherence_root(tmp_path, ACCOUNT_A)

    class SettleRaisesDirt:
        async def __call__(self, seconds: float) -> None:
            # Raised after the cut was captured: generation 1 > cut (0).
            s02_set_barrier(paths, 1, active=True)
            s02_set_external_dirt(paths, 1, active=True)
            await asyncio.sleep(0)

    outcome = await run_full_refresh(
        paths=paths,
        fetch=s02_fetch(s02_make_export([s02_node("a")])),
        settle_seconds=2.0,
        snapshot_dir=_snapshot_dir(tmp_path),
        sleep=SettleRaisesDirt(),
        poll_seconds=0.02,
    )
    assert outcome.status == "published"
    assert outcome.hard_barrier_cleared is False
    assert outcome.external_dirt_cleared is False
    manifest = read_manifest(paths, required=True)
    assert manifest.dirty_status.active is True
    assert manifest.dirty_status.generation == 1
    dirt = read_external_dirt(paths)
    assert dirt is not None and dirt.active is True and dirt.generation == 1


@async_test
async def test_signal_during_download_survives(tmp_path):
    paths = make_coherence_root(tmp_path, ACCOUNT_A)

    async def fetch():
        s02_set_barrier(paths, 2, active=True)
        return s02_make_export([s02_node("a")])

    outcome = await run_full_refresh(
        paths=paths,
        fetch=fetch,
        settle_seconds=0,
        snapshot_dir=_snapshot_dir(tmp_path),
        sleep=RecordedSleep(),
        poll_seconds=0.02,
    )
    assert outcome.status == "published"
    assert outcome.hard_barrier_cleared is False
    manifest = read_manifest(paths, required=True)
    assert manifest.dirty_status.active is True
    assert manifest.dirty_status.generation == 2


@async_test
async def test_pre_cut_barrier_and_dirt_clear_on_success(tmp_path):
    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    s02_set_barrier(paths, 3, active=True)
    s02_set_external_dirt(paths, 4, active=True)
    outcome = await run_full_refresh(
        paths=paths,
        fetch=s02_fetch(s02_make_export([s02_node("a")])),
        refresh_kind="recovery",
        settle_seconds=0,
        snapshot_dir=_snapshot_dir(tmp_path),
        sleep=RecordedSleep(),
        poll_seconds=0.02,
    )
    assert outcome.hard_barrier_cleared is True
    assert outcome.external_dirt_cleared is True
    manifest = read_manifest(paths, required=True)
    assert manifest.dirty_status.active is False
    # The generation counter is PRESERVED on clear (monotonic reference).
    assert manifest.dirty_status.generation == 3
    dirt = read_external_dirt(paths)
    assert dirt is not None and dirt.active is False and dirt.generation == 4


@async_test
async def test_failed_fetch_clears_nothing_and_preserves_state(tmp_path):
    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    s02_set_barrier(paths, 1, active=True)
    s02_set_external_dirt(paths, 1, active=True)
    before = read_manifest(paths, required=True)

    async def failing_fetch():
        raise CoherenceError("simulated transport failure")

    with pytest.raises(CoherenceError):
        await run_full_refresh(
            paths=paths,
            fetch=failing_fetch,
            settle_seconds=0,
            snapshot_dir=_snapshot_dir(tmp_path),
            sleep=RecordedSleep(),
            poll_seconds=0.02,
        )
    after = read_manifest(paths, required=True)
    # A failed refresh never clears the old manifest, barrier, or dirt.
    assert after.epoch_id == before.epoch_id
    assert after.epoch_number == before.epoch_number
    assert after.dirty_status.active is True
    dirt = read_external_dirt(paths)
    assert dirt is not None and dirt.active is True
    claim = _read_publication_record(paths)
    assert claim is not None and claim.status == "failed"


@async_test
async def test_failed_fetch_leaves_no_candidate_residue(tmp_path):
    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    snapshot_dir = _snapshot_dir(tmp_path)

    async def failing_fetch():
        raise CoherenceError("simulated transport failure")

    with pytest.raises(CoherenceError):
        await run_full_refresh(
            paths=paths,
            fetch=failing_fetch,
            settle_seconds=0,
            snapshot_dir=snapshot_dir,
            sleep=RecordedSleep(),
            poll_seconds=0.02,
        )
    assert list(snapshot_dir.iterdir()) == []
