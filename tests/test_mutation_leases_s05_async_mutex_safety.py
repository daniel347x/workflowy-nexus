"""Warning-only retirement observes a test-held in-process lock without reads."""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import pytest
from coherence_harness import s04_checkpoint_config
from mutation_lease_harness import (
    ACCOUNT_A, append_tx, effects_create, make_lease_env, node,
)

from workflowy_mcp import carto_job_governance as gov
from workflowy_mcp.coherence.checkpoint import run_checkpoint_rotation
from workflowy_mcp.coherence.journal_store import retire_incorporated_journal_records
from workflowy_mcp.coherence.paths import coherence_lock_async, journal_record_filename
from workflowy_mcp.coherence.schema import read_manifest


class _Ticker:
    """A 10ms event-loop ticker: if the loop stalls, ticks stop accumulating."""

    def __init__(self) -> None:
        self.ticks = 0
        self._stop = asyncio.Event()
        self.task: asyncio.Task | None = None

    async def _run(self) -> None:
        while not self._stop.is_set():
            self.ticks += 1
            await asyncio.sleep(0.01)

    async def __aenter__(self) -> "_Ticker":
        self.task = asyncio.create_task(self._run())
        await asyncio.sleep(0.12)
        return self

    async def __aexit__(self, *exc) -> None:
        self._stop.set()
        await self.task


def test_retirement_defers_nonblocking_when_coherence_mutex_held(tmp_path, monkeypatch):
    """A test task holds the short lock; defer before reading or enumerating."""
    from workflowy_mcp.coherence import journal_store
    env = make_lease_env(tmp_path, ACCOUNT_A)
    record, _manifest = append_tx(env, "1" * 32, effects_create(node("committed")))
    record_path = env.paths.journal_dir / journal_record_filename(record.sequence)

    def keep_record(path):
        if path == record_path:
            raise OSError("injected warning-only journal residue")
        return gov._default_unlink(path)

    async def scenario():
        result = await run_checkpoint_rotation(
            env.paths, snapshot_dir=env.snapshot_dir,
            config=s04_checkpoint_config(record_threshold=1),
            fs_ops=gov.DurableFileOps(unlink=keep_record),
        )
        assert result.status == "published", result.warnings
        assert record.sequence < read_manifest(env.paths, required=True).journal_floor_seq
        assert record_path.exists()
        async with coherence_lock_async(env.paths):
            async with _Ticker() as ticker:
                reads = []
                real_read, real_iterdir = journal_store.read_manifest, Path.iterdir
                def observe_read(*args, **kwargs):
                    reads.append("manifest")
                    return real_read(*args, **kwargs)
                def observe_iterdir(path):
                    if path == env.paths.journal_dir:
                        reads.append("journal")
                    return real_iterdir(path)
                with monkeypatch.context() as patch:
                    patch.setattr(journal_store, "read_manifest", observe_read)
                    patch.setattr(Path, "iterdir", observe_iterdir)
                    if os.environ.get("CUT2B_NEUTRALIZE_RETIREMENT_BUSY"):
                        patch.setattr(journal_store, "_coherence_lock_for", lambda paths: asyncio.Lock())
                    before = time.monotonic()
                    retired, warnings = retire_incorporated_journal_records(env.paths)
                    elapsed = time.monotonic() - before
                assert reads == [], "busy retirement read state before deferring"
                ticks_at_call = ticker.ticks
                await asyncio.sleep(0.05)
                assert ticker.ticks > ticks_at_call
                assert elapsed < 5.0
                assert retired == 0
                assert any("coherence lock busy" in warning for warning in warnings)
                assert not any("failed" in warning for warning in warnings)
                assert record_path.exists()
            return ticker.ticks

    assert asyncio.run(scenario()) >= 5
    retired, warnings = retire_incorporated_journal_records(env.paths)
    assert warnings == []
    assert retired == 1
    assert not record_path.exists()


def test_retirement_nonblocking_sensitivity(tmp_path, monkeypatch):
    from workflowy_mcp.coherence import journal_store

    with monkeypatch.context() as patch:
        patch.setattr(journal_store, "_coherence_lock_for", lambda paths: asyncio.Lock())
        with pytest.raises(AssertionError, match="busy retirement read state"):
            test_retirement_defers_nonblocking_when_coherence_mutex_held(tmp_path / "neutralized", monkeypatch)
    test_retirement_defers_nonblocking_when_coherence_mutex_held(tmp_path / "restored", monkeypatch)
