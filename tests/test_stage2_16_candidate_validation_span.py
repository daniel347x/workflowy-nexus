"""Stage2-16: single strict parse outside the short in-process coherence lock.

Retain error precedence, actual epoch/base publication, exactly one open,
zero candidate stat/read under the lock, original large-envelope ratio and
six real same-loop appends. The retired TOCTOU identity oracle is absent.
"""

from __future__ import annotations

import asyncio
import os
import time
from contextlib import asynccontextmanager
from functools import wraps
from pathlib import Path

import pytest
from test_cut_2a_publication import _publication_records_cleanup
from coherence_harness import (
    ACCOUNT_A,
    NOW,
    make_coherence_root,
    seed_publication_record,
    s02_append,
    s02_fetch,
    s02_make_export,
    RecordedSleep,
    s02_node,
    touch,
    wait_for_file,
)

import workflowy_mcp.coherence.epoch_publisher as epoch_publisher
from workflowy_mcp.coherence import refresh_authority as ra
from workflowy_mcp.coherence import (
    new_epoch_id,
    publish_epoch,
    read_manifest,
    run_full_refresh,
)
from workflowy_mcp.coherence.errors import (
    CoherenceSchemaError,
    CoherenceStateMissingError,
    ManifestConflictError,
)
from workflowy_mcp.coherence.snapshot_store import (
    new_snapshot_candidate_path,
    write_snapshot_candidate,
)

REPO = Path(__file__).resolve().parents[1]
_API_BASE_URL = "https://stage2-16.invalid"
_API_KEY = "stage2-16-key"


def async_test(function):
    @wraps(function)
    def wrapper(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))

    return wrapper


def _snapshot_dir(tmp_path: Path) -> Path:
    target = tmp_path / "snapshots"
    target.mkdir(parents=True, exist_ok=True)
    return target


def _plant_full_refresh_claim(paths, *, target_epoch_id: str | None = None):
    """Seed one active full-refresh record whose in-memory cut matches the
    current manifest (the A5.2 self-defense guards require it)."""

    return seed_publication_record(
        paths,
        status="publishing",
        requested_hard_generation=0,
        requested_external_generation=0,
        target_epoch_id=target_epoch_id or new_epoch_id(),
        started_at=NOW,
        updated_at=NOW,
    )


def _write_candidate(paths, snapshot_dir: Path, claim, *, nodes, incorporated: int = 0):
    return write_snapshot_candidate(
        new_snapshot_candidate_path(snapshot_dir),
        {"nodes": nodes},
        account_scope_id=ACCOUNT_A,
        epoch_number=claim.target_epoch_number,
        epoch_id=claim.target_epoch_id,
        incorporated_through_seq=incorporated,
        publication_kind="full_refresh",
    )


async def _publish(paths, claim, candidate, *, incorporated: int = 0, claim_id=None):
    return await publish_epoch(
        paths=paths,
        claim_id=claim_id or claim.claim_id,
        basis_epoch_id=read_manifest(paths, required=True).epoch_id,
        candidate_snapshot_path=candidate,
        incorporated_through_seq=incorporated,
        publication_kind="full_refresh",
        refresh_cut=claim.cut_token,
        clear_hard_through_generation=None,
        clear_external_through_generation=None,
    )


# ---------------------------------------------------------------------------
# Happy path + error vocabulary / precedence
# ---------------------------------------------------------------------------


@async_test
async def test_publish_succeeds_with_outside_lock_candidate_validation(tmp_path, monkeypatch):
    """Baseline: publication through the restructured span succeeds and the
    epoch advances exactly once."""
    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    claim = _plant_full_refresh_claim(paths)
    candidate = _write_candidate(paths, _snapshot_dir(tmp_path), claim, nodes=[s02_node("a")])
    reads = []
    real_read = epoch_publisher.read_snapshot_envelope

    def counted_read(path):
        reads.append(str(path))
        return real_read(path)

    monkeypatch.setattr(epoch_publisher, "read_snapshot_envelope", counted_read)
    if os.environ.get("CUT2B_NEUTRALIZE_SINGLE_PARSE"):
        real_lock = epoch_publisher.coherence_lock_async

        @asynccontextmanager
        async def doubled_read(paths_arg):
            counted_read(candidate)
            async with real_lock(paths_arg):
                yield

        monkeypatch.setattr(epoch_publisher, "coherence_lock_async", doubled_read)
    result = await _publish(paths, claim, candidate)
    manifest = read_manifest(paths, required=True)
    assert result.epoch_number == 1
    assert manifest.epoch_number == 1
    assert manifest.base_snapshot_path == candidate
    assert reads == [candidate], f"candidate must parse exactly once: {reads}"


@async_test
async def test_missing_candidate_keeps_state_missing_vocabulary(tmp_path, monkeypatch):
    """The Stage2-09 shape: the candidate is gone BEFORE publish_epoch is
    entered — the failure is the same CoherenceStateMissingError as when
    the read lived inside the lock."""
    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    claim = _plant_full_refresh_claim(paths)
    candidate = _write_candidate(paths, _snapshot_dir(tmp_path), claim, nodes=[s02_node("a")])
    os.unlink(candidate)
    if os.environ.get("CUT2B_NEUTRALIZE_MISSING_CANDIDATE"):
        from workflowy_mcp.coherence.snapshot_store import read_snapshot_envelope
        saved = _write_candidate(paths, _snapshot_dir(tmp_path), claim, nodes=[s02_node("a")])
        envelope = read_snapshot_envelope(saved)
        monkeypatch.setattr(epoch_publisher, "read_snapshot_envelope", lambda path: envelope)
    with pytest.raises(CoherenceStateMissingError):
        await _publish(paths, claim, candidate)
    assert read_manifest(paths, required=True).epoch_number == 0


@async_test
async def test_claim_guards_fire_before_candidate_error(tmp_path):
    """Error precedence preserved: a claim-token mismatch wins over a
    missing candidate, exactly as with the in-lock read."""
    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    claim = _plant_full_refresh_claim(paths)
    candidate = _write_candidate(paths, _snapshot_dir(tmp_path), claim, nodes=[s02_node("a")])
    os.unlink(candidate)
    with pytest.raises(ManifestConflictError):
        await _publish(paths, claim, candidate, claim_id="0" * 32)
    assert read_manifest(paths, required=True).epoch_number == 0


# ---------------------------------------------------------------------------
# Structural regression lock — no candidate read under the coherence mutex
# ---------------------------------------------------------------------------


@async_test
async def test_no_candidate_read_under_coherence_mutex(tmp_path, monkeypatch):
    """Exactly one candidate open outside the lock; zero candidate stats."""
    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    claim = _plant_full_refresh_claim(paths)
    snap_dir = _snapshot_dir(tmp_path)
    candidate = _write_candidate(paths, snap_dir, claim, nodes=[s02_node("a")])

    state = {"held": False, "opens": [], "stats": []}
    real_lock = epoch_publisher.coherence_lock_async

    @asynccontextmanager
    async def _recording_lock(paths_arg):
        async with real_lock(paths_arg):
            state["held"] = True
            try:
                if os.environ.get("CUT2B_NEUTRALIZE_PARSE_INSIDE_LOCK"):
                    epoch_publisher.read_snapshot_envelope(candidate)
                yield
            finally:
                state["held"] = False

    real_open = Path.open
    real_stat = Path.stat

    def _open(self, *args, **kwargs):
        if os.path.normpath(str(self)) == os.path.normpath(candidate):
            state["opens"].append(state["held"])
        return real_open(self, *args, **kwargs)

    def _stat(self, *args, **kwargs):
        if os.path.normpath(str(self)) == os.path.normpath(candidate):
            state["stats"].append(state["held"])
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(epoch_publisher, "coherence_lock_async", _recording_lock)
    monkeypatch.setattr(Path, "open", _open)
    monkeypatch.setattr(Path, "stat", _stat)

    result = await _publish(paths, claim, candidate)
    assert result.epoch_number == 1
    assert state["stats"] == [], f"candidate stat returned: {state['stats']}"
    assert state["opens"] == [False], (
        f"candidate file was read under the coherence mutex: {state['opens']}"
    )


@async_test
async def test_in_lock_span_dwarfs_outside_read_span(tmp_path, monkeypatch):
    """Latency regression proof (relative, machine-independent): with a
    ~20 MB candidate, the ENTIRE in-lock publish section costs a small
    fraction of the outside candidate reparse — the span that used to
    hold the mutex through the full JSON parse."""
    paths = make_coherence_root(tmp_path, ACCOUNT_A)
    # ~64 MB candidate: sized so the JSON parse (~0.2 s) dominates the
    # near-constant in-lock residual (~10 ms of small-file I/O) by an
    # order of magnitude on any plausible host.
    node_count = 200_000
    payload_nodes = [
        s02_node(f"n{i}", name=f"node-{i}", note="x" * 240) for i in range(node_count)
    ]
    claim = _plant_full_refresh_claim(paths)
    snap_dir = _snapshot_dir(tmp_path)
    candidate = _write_candidate(paths, snap_dir, claim, nodes=payload_nodes)
    size_mb = os.path.getsize(candidate) / 1e6

    spans = {"outside_read": 0.0, "in_lock": 0.0}
    real_lock = epoch_publisher.coherence_lock_async
    real_read = epoch_publisher.read_snapshot_envelope

    @asynccontextmanager
    async def _timed_lock(paths_arg):
        start = time.perf_counter()
        async with real_lock(paths_arg):
            if os.environ.get("CUT2B_NEUTRALIZE_PARSE_INSIDE_LOCK"):
                real_read(candidate)
            yield
        spans["in_lock"] += time.perf_counter() - start

    def _timed_read(candidate_arg):
        start = time.perf_counter()
        envelope = real_read(candidate_arg)
        spans["outside_read"] += time.perf_counter() - start
        return envelope

    monkeypatch.setattr(epoch_publisher, "coherence_lock_async", _timed_lock)
    monkeypatch.setattr(epoch_publisher, "read_snapshot_envelope", _timed_read)

    result = await _publish(paths, claim, candidate)
    assert result.epoch_number == 1
    assert spans["outside_read"] > 0 and spans["in_lock"] > 0
    # The pre-Stage2-16 shape held the mutex for ~the full parse.  The new
    # in-lock section (small record/manifest reads + CAS write) must
    # stay below 10% of the outside reparse — a 10x margin that cannot
    # flake against machine speed, only against a genuine regression (a
    # candidate read back under the lock pushes the ratio toward 1x).
    assert spans["in_lock"] * 10 < spans["outside_read"], (
        f"in-lock span {spans['in_lock']:.3f}s not <10x smaller than the "
        f"outside reparse {spans['outside_read']:.3f}s (candidate {size_mb:.1f} MB)"
    )


# ---------------------------------------------------------------------------
# In-process publisher/appender contention latency proof
# ---------------------------------------------------------------------------

@async_test
async def test_real_two_process_publisher_contention(tmp_path, monkeypatch):
    """Historical name: two tasks rendezvous after parse, before actual lock.

    Six real appends, original span/latency bounds and zero candidate opens
    under the coherence mutex remain the oracles; only PID geometry retires.
    """
    root = tmp_path / "share"
    root.mkdir(parents=True)
    paths = make_coherence_root(root / "w", ACCOUNT_A)
    parse_ready, first_append = asyncio.Event(), asyncio.Event()
    state = {"held": False, "in_lock": 0.0, "candidate_opens_under_lock": 0}
    real_lock = epoch_publisher.coherence_lock_async
    real_open = Path.open

    @asynccontextmanager
    async def timed_lock(paths_arg):
        # publish_epoch has finished its single strict parse at this seam.
        parse_ready.set()
        await first_append.wait()
        start = time.perf_counter()
        state["held"] = True
        try:
            async with real_lock(paths_arg):
                if os.environ.get("CUT2B_NEUTRALIZE_PARSE_INSIDE_LOCK"):
                    epoch_publisher.read_snapshot_envelope(
                        ra._read_publication_record(paths_arg).candidate_snapshot_path
                    )
                yield
        finally:
            state["held"] = False
            state["in_lock"] += time.perf_counter() - start

    def counted_open(path, *args, **kwargs):
        if state["held"] and "snapshot" in str(path):
            state["candidate_opens_under_lock"] += 1
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(epoch_publisher, "coherence_lock_async", timed_lock)
    monkeypatch.setattr(Path, "open", counted_open)

    async def append_six():
        await parse_ready.wait()
        elapsed = []
        for _ in range(6):
            start = time.perf_counter()
            s02_append(paths)
            elapsed.append(time.perf_counter() - start)
            first_append.set()
            await asyncio.sleep(0)
        return {"appended": len(elapsed), "max_elapsed": max(elapsed)}

    nodes = [s02_node(f"n{i}", name=f"node-{i}", note="x" * 240) for i in range(200_000)]
    publisher = asyncio.create_task(run_full_refresh(
        paths=paths, fetch=s02_fetch(s02_make_export(nodes)), refresh_kind="system",
        settle_seconds=0.0, snapshot_dir=str(root / "snap"), sleep=RecordedSleep(), poll_seconds=0.05,
    ))
    appender = asyncio.create_task(append_six())
    try:
        outcome, app = await asyncio.wait_for(asyncio.gather(publisher, appender), 300)
        pub = {
            "published": outcome is not None and outcome.status == "published",
            "epoch_number": None if outcome is None else outcome.epoch_number,
            "in_lock_seconds": state["in_lock"],
            "candidate_opens_under_lock": state["candidate_opens_under_lock"],
        }

        assert pub["published"] is True
        assert pub["epoch_number"] == 1
        assert pub["candidate_opens_under_lock"] == 0, (
            "the publisher re-read the candidate under the coherence mutex "
            f"({pub['candidate_opens_under_lock']} times)"
        )
        # Coarse latency corroboration (the deterministic lock is the zero
        # reads above): ~64 MB candidate → the old in-lock parse was ~0.2 s;
        # the new in-lock window must stay far below it even on a slow host.
        assert pub["in_lock_seconds"] < 0.15, (
            f"publisher in-lock window {pub['in_lock_seconds']:.3f}s"
        )
        assert app["appended"] == 6
        assert app["max_elapsed"] < 2.0

    finally:
        await asyncio.gather(publisher, appender, return_exceptions=True)
        ra._PUBLICATION_RECORDS.pop(ra._publication_key(paths), None)
