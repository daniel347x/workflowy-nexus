"""S02 §16.1 — in-memory publication, dedupe, and successor tests."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from functools import wraps
from pathlib import Path

import pytest
from test_cut_2a_publication import _publication_records_cleanup
from coherence_harness import (
    ACCOUNT_A,
    NOW,
    RecordedSleep,
    make_coherence_root,
    s02_fetch,
    s02_make_export,
    s02_node,
    s02_set_barrier,
    seed_publication_record,
)

from workflowy_mcp.coherence import (
    COMPAT_PATCH_KIND,
    FULL_REFRESH_KINDS,
    RefreshCutToken,
    read_manifest,
    run_full_refresh,
)
from workflowy_mcp.coherence.errors import RefreshClaimError
from workflowy_mcp.coherence.refresh_authority import (
    _read_publication_record,
    _store_publication_record,
    _write_claim_status,
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


def _fake_claim(
    paths,
    *,
    status: str = "downloading",
    cut_hard: int = 0,
    cut_external: int = 0,
    target_epoch_number: int | None = None,
    target_epoch_id: str | None = None,
    refresh_kind: str = "system",
):
    manifest = read_manifest(paths, required=True)
    targets = {}
    if target_epoch_number is not None:
        targets["target_epoch_number"] = target_epoch_number
    if target_epoch_id is not None:
        targets["target_epoch_id"] = target_epoch_id
    return seed_publication_record(
        paths,
        refresh_kind=refresh_kind,
        status=status,
        cut_token=RefreshCutToken(
            account_scope_id=ACCOUNT_A,
            epoch_number=manifest.epoch_number,
            epoch_id=manifest.epoch_id,
            journal_head_seq=manifest.journal_head_seq,
            hard_barrier_generation=cut_hard,
            external_dirt_generation=cut_external,
            captured_at=NOW,
        ) if refresh_kind in FULL_REFRESH_KINDS else None,
        requested_hard_generation=0,
        requested_external_generation=0,
        started_at=NOW,
        updated_at=NOW,
        **targets,
    )


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------


@async_test
async def test_refresh_claim_roundtrip_and_terminal(tmp_path):
    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    claim = _fake_claim(paths)
    decoded = _read_publication_record(paths)
    assert decoded is claim
    assert not decoded.is_terminal
    for status in ("completed", "failed"):
        terminal = await _write_claim_status(paths, claim, status)
        assert terminal.is_terminal
        assert _read_publication_record(paths) is terminal


# ---------------------------------------------------------------------------
# Acquisition / dedupe / successor semantics
# ---------------------------------------------------------------------------


@async_test
async def test_single_full_refresh_publishes_epoch_and_terminal_claim(tmp_path):
    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    calls: list[int] = []
    sleep = RecordedSleep()
    payload = s02_make_export([s02_node("a"), s02_node("b", parent_id="a")])
    outcome = await run_full_refresh(
        paths=paths,
        fetch=s02_fetch(payload, calls),
        refresh_kind="system",
        settle_seconds=0,
        snapshot_dir=_snapshot_dir(tmp_path),
        sleep=sleep,
    )
    assert outcome.status == "published"
    assert calls == [1]
    assert outcome.epoch_number == 1
    manifest = read_manifest(paths, required=True)
    assert manifest.epoch_number == 1
    assert manifest.previous_epoch_id is not None
    assert manifest.epoch_id != manifest.previous_epoch_id
    claim = _read_publication_record(paths)
    assert claim is not None and claim.status == "completed"
    assert claim.result and claim.result.get("epoch_id") == manifest.epoch_id
    # Local adoption payload is fully materialized and canonical.
    assert {n["id"] for n in outcome.nodes_export["nodes"]} == {"a", "b"}


@async_test
async def test_join_covered_active_claim_dedupes(tmp_path):
    """A request whose generations are covered by the ACTIVE claim joins it:
    no second fetch, both callers succeed (in-process deterministic join)."""
    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    # The active claim pretends its publication completed the CURRENT epoch.
    manifest = read_manifest(paths, required=True)
    active = _fake_claim(
        paths,
        status="completed",
        target_epoch_number=manifest.epoch_number,
        target_epoch_id=manifest.epoch_id,
    )
    calls: list[int] = []
    # Pretend a base exists for the joiner's local adoption: write the base
    # the fake claim 'published'.  The manifest base is None here (epoch 0
    # bootstrap), so point it at a real wrapped base first.
    from workflowy_mcp.coherence import manifest_with_updates, write_manifest
    from workflowy_mcp.coherence.snapshot_store import (
        new_snapshot_candidate_path,
        write_snapshot_candidate,
    )

    base_path = write_snapshot_candidate(
        new_snapshot_candidate_path(_snapshot_dir(tmp_path)),
        {"nodes": [s02_node("root")]},
        account_scope_id=ACCOUNT_A,
        epoch_number=manifest.epoch_number,
        epoch_id=manifest.epoch_id,
        incorporated_through_seq=0,
        publication_kind="full_refresh",
    )
    write_manifest(
        paths,
        manifest_with_updates(
            manifest, base_snapshot_path=base_path, coherence_mode="transition"
        ),
        expected_epoch_id=manifest.epoch_id,
        expected_head_seq=manifest.journal_head_seq,
    )
    # The active claim is already terminal-completed; a fresh request JOINS
    # only while it is ACTIVE, so re-mark it active without touching lineage.
    await _write_claim_status(paths, active, "downloading")
    claim = _read_publication_record(paths)
    assert claim is not None and not claim.is_terminal

    async def complete_later() -> None:
        await asyncio.sleep(0.2)
        current = _read_publication_record(paths)
        assert current is not None
        await _write_claim_status(paths, current, "completed")

    completer = asyncio.create_task(complete_later())
    outcome = await run_full_refresh(
        paths=paths,
        fetch=s02_fetch(s02_make_export([]), calls),
        refresh_kind="widget",
        settle_seconds=0,
        snapshot_dir=_snapshot_dir(tmp_path),
        sleep=RecordedSleep(),
        poll_seconds=0.02,
    )
    await completer
    assert outcome.status == "joined"
    assert outcome.deduplicated is True
    assert calls == []  # no second download
    assert outcome.node_count == 1


@async_test
async def test_newer_generation_serializes_exactly_one_successor(tmp_path, monkeypatch):
    """A request requiring a generation NEWER than the active cut waits for
    the active publisher, then acquires exactly one successor refresh."""
    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    s02_set_barrier(paths, 1, active=True)
    # Active full-refresh claim whose cut (0,0) does NOT cover required gen 1.
    active = _fake_claim(paths, status="downloading", cut_hard=0, cut_external=0)
    calls: list[int] = []

    async def fail_later() -> None:
        await asyncio.sleep(0.2)
        await _write_claim_status(paths, active, "failed")

    completer = asyncio.create_task(fail_later())
    import workflowy_mcp.coherence.refresh_authority as authority
    joined = asyncio.Event()
    real_join = authority._join_refresh
    async def observe_join(*args, **kwargs):
        joined.set()
        return await real_join(*args, **kwargs)
    monkeypatch.setattr(authority, "_join_refresh", observe_join)
    async def successor_fetch():
        calls.append(1)
        assert calls == [1], "uncovered contenders started multiple successor fetches"
        await joined.wait()
        return s02_make_export([s02_node("x")])
    async def contender():
        recorded_sleep = RecordedSleep()
        async def observe_wait(seconds):
            current = _read_publication_record(paths)
            if seconds > 0 and current is not None and current.claim_id != active.claim_id:
                # A broken no-join decision must not deadlock the fetch gate:
                # observing the active successor is sufficient acknowledgment.
                joined.set()
            await recorded_sleep(seconds)
        return await run_full_refresh(
        paths=paths,
        fetch=successor_fetch,
        refresh_kind="recovery",
        settle_seconds=0,
        snapshot_dir=_snapshot_dir(tmp_path),
        sleep=observe_wait,
        poll_seconds=0.02,
        )
    outcomes = await asyncio.gather(contender(), contender())
    outcome = next(item for item in outcomes if item.status == "published")
    assert sorted(item.status for item in outcomes) == ["joined", "published"]
    await completer
    assert outcome.status == "published"
    assert outcome.successor_refresh_performed is True
    assert calls == [1]  # exactly one successor download
    # The successor's cut covered barrier gen 1 and cleared it.
    manifest = read_manifest(paths, required=True)
    assert manifest.dirty_status.active is False
    assert outcome.hard_barrier_cleared is True


@async_test
async def test_compat_patch_never_dedupes_with_full_refresh(tmp_path):
    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    _fake_claim(paths, status="downloading")  # active full-refresh claim
    from workflowy_mcp.coherence import run_compat_patch

    with pytest.raises(RefreshClaimError):
        await run_compat_patch(
            paths=paths,
            patches=[{"id": "a", "note": "x"}],
            snapshot_dir=_snapshot_dir(tmp_path),
            sleep=RecordedSleep(),
            poll_seconds=0.02,
            max_wait_seconds=0.2,
        )


@async_test
async def test_full_refresh_waits_for_active_compat_patch(tmp_path, monkeypatch):
    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    _fake_claim(paths, status="publishing", refresh_kind=COMPAT_PATCH_KIND)
    calls: list[int] = []
    # DI40KK: the retained join deadline masks a wrong join's completion;
    # observe the prohibited acquisition decision without changing that budget.
    import workflowy_mcp.coherence.refresh_authority as authority
    join_entries = []
    real_join = authority._join_refresh
    async def observe_join(*args, **kwargs):
        join_entries.append(kwargs["joined"].claim_id)
        return await real_join(*args, **kwargs)
    monkeypatch.setattr(authority, "_join_refresh", observe_join)
    with pytest.raises(RefreshClaimError):
        await run_full_refresh(
            paths=paths,
            fetch=s02_fetch(s02_make_export([]), calls),
            settle_seconds=0,
            snapshot_dir=_snapshot_dir(tmp_path),
            sleep=RecordedSleep(),
            poll_seconds=0.02,
            max_wait_seconds=0.2,
        )
    assert calls == []
    assert join_entries == []


# ---------------------------------------------------------------------------
# Consecutive publication and exact-token behavior
# ---------------------------------------------------------------------------


@async_test
async def test_no_cooldown_between_refreshes(tmp_path):
    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    for expected_epoch in (1, 2):
        outcome = await run_full_refresh(
            paths=paths,
            fetch=s02_fetch(s02_make_export([s02_node("n")])),
            settle_seconds=0,
            snapshot_dir=_snapshot_dir(tmp_path),
            sleep=RecordedSleep(),
            poll_seconds=0.02,
        )
        assert outcome.epoch_number == expected_epoch
        assert outcome.status == "published"


@async_test
async def test_exact_token_guard_rejects_foreign_phase_write(tmp_path):
    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    claim = _fake_claim(paths, status="downloading")
    from workflowy_mcp.coherence.errors import ManifestConflictError

    with pytest.raises(ManifestConflictError):
        _store_publication_record(
            paths, replace(claim, status="settling"), expected_claim_id="not-the-claim"
        )


@async_test
async def test_final_new_fetch_waits_older_download_and_owns_successor(tmp_path, monkeypatch):
    """The real second caller lands AFTER old download, BEFORE old publication."""
    import workflowy_mcp.coherence.refresh_authority as authority
    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    downloaded, release = asyncio.Event(), asyncio.Event()
    events = []
    real_publish = authority.publish_epoch

    async def paused_publish(**kwargs):
        if not downloaded.is_set():
            downloaded.set()
            await release.wait()
        events.append("publish")
        return await real_publish(**kwargs)
    monkeypatch.setattr(authority, "publish_epoch", paused_publish)
    async def old_fetch():
        events.append("old_fetch")
        return s02_make_export([s02_node("old")])
    async def final_fetch():
        events.append("final_fetch")
        return s02_make_export([s02_node("last")])
    old = asyncio.create_task(run_full_refresh(paths=paths, fetch=old_fetch,
        settle_seconds=0, snapshot_dir=_snapshot_dir(tmp_path), poll_seconds=0.001))
    await downloaded.wait()
    events.append("positive_stop")
    final = asyncio.create_task(run_full_refresh(paths=paths, fetch=final_fetch,
        require_new_fetch=True, settle_seconds=0, snapshot_dir=_snapshot_dir(tmp_path), poll_seconds=0.001))
    await asyncio.sleep(0.02)
    assert "final_fetch" not in events
    release.set()
    previous, outcome = await asyncio.gather(old, final)
    assert outcome.status == "published" and not outcome.deduplicated
    assert outcome.claim_id != previous.claim_id
    assert events == ["old_fetch", "positive_stop", "publish", "final_fetch", "publish"]
    assert set(n["id"] for n in outcome.nodes_export["nodes"]) == {"last"}


@async_test
async def test_final_demand_still_obeys_wait_bound_and_cancel(tmp_path, monkeypatch):
    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    _fake_claim(paths)
    calls = []
    # DI40KK's same-shape ruling: joining an immortal fixture owner hits
    # the retained join deadline too, so additionally observe no join entry.
    import workflowy_mcp.coherence.refresh_authority as authority
    join_entries = []
    real_join = authority._join_refresh
    async def observe_join(*args, **kwargs):
        join_entries.append(kwargs["joined"].claim_id)
        return await real_join(*args, **kwargs)
    monkeypatch.setattr(authority, "_join_refresh", observe_join)
    with pytest.raises(RefreshClaimError):
        await run_full_refresh(paths=paths, fetch=s02_fetch(s02_make_export([]), calls),
            require_new_fetch=True, settle_seconds=0, snapshot_dir=_snapshot_dir(tmp_path),
            poll_seconds=0.001, max_wait_seconds=0.01)
    def cancelled():
        raise asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await run_full_refresh(paths=paths, fetch=s02_fetch(s02_make_export([]), calls),
            require_new_fetch=True, settle_seconds=0, snapshot_dir=_snapshot_dir(tmp_path),
            cancel_check=cancelled)
    assert calls == []
    assert join_entries == []
